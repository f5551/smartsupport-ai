"""Read-only preflight of Git-distributable files. Never prints secret values.

Uses a disposable Git index/config, so it also works before git init and honors
the actual .gitignore. This pattern scan is not a guarantee of secret absence.
PDF scanning requires pypdf (locally verified with 7.0.0) or working Poppler
pdftotext on PATH. Missing extraction support or unreadable PDF text fails closed.
Extracted text is scanned; this is not OCR or an embedded-file security audit.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
TRACES = ("success_1.jsonl", "near_failure_recovery.jsonl", "explicit_failure.jsonl")
PATTERNS = {
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "aws_key": re.compile(r"AKIA[A-Z0-9]{16}"),
}
# Exact known synthetic regression fixtures only; other test secrets still fail.
FIXTURES = {
    "tests/test_audit_logger.py": {"sk-this-is-a-test-secret-not-a-real-key"},
    "tests/test_streamlit_app.py": {"sk-synthetic-private-key-for-redaction"},
    "tests/test_web_runtime.py": {"sk-this-is-a-synthetic-secret-to-block"},
    "release_check.py": {"sk-this-is-a-test-secret-not-a-real-key",
                         "sk-synthetic-private-key-for-redaction",
                         "sk-this-is-a-synthetic-secret-to-block"},
}


def distributable_files(root=ROOT):
    root = Path(root).resolve()
    with tempfile.TemporaryDirectory(prefix="smartsupport-git-check-") as temporary:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "--quiet", temporary], check=True, env=env,
                       capture_output=True)
        git = ["git", f"--git-dir={Path(temporary) / '.git'}", f"--work-tree={root}"]

        def ignored(paths):
            if not paths:
                return set()
            result = subprocess.run(git + ["check-ignore", "--no-index", "-z", "--stdin"],
                                    input="\0".join(paths).encode() + b"\0",
                                    capture_output=True, env=env, cwd=root)
            if result.returncode not in {0, 1}:
                raise RuntimeError("Git ignore validation failed")
            return set(result.stdout.decode().strip("\0").split("\0"))

        files = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            base = Path(directory)
            dirs[:] = [d for d in dirs if d != ".git" and not (base / d).is_symlink()]
            candidates = [(base / d).relative_to(root).as_posix() for d in dirs]
            excluded = ignored(candidates)
            dirs[:] = [d for d in dirs if (base / d).relative_to(root).as_posix() not in excluded]
            candidates = [(base / n).relative_to(root).as_posix() for n in names]
            excluded = ignored(candidates)
            files.extend(p for p in candidates if p not in excluded and not (root / p).is_symlink())
        return sorted(files)


def secret_findings(text, relative_path):
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in PATTERNS.items():
            if any(m.group() not in FIXTURES.get(relative_path, set()) for m in pattern.finditer(line)):
                findings.append({"path": relative_path, "line": number, "rule": rule})
    return findings


class PDFScanError(RuntimeError):
    """Safe diagnostic: never include extracted text or parser stderr."""


def pdf_text(path):
    # pypdf is a release-tool dependency, not an application/model dependency.
    # If absent, retain support for a system Poppler installation.
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None
    if PdfReader is not None:
        try:
            with path.open("rb") as source:
                reader = PdfReader(source, strict=True)
                if reader.is_encrypted:
                    raise PDFScanError("Encrypted PDF cannot be scanned without an explicit review")
                pages = [page.extract_text() or "" for page in reader.pages]
                if not pages or any(not page.strip() for page in pages):
                    raise PDFScanError("PDF contains a page without extractable text; OCR or manual security review is required")
                return "\n".join(pages)
        except PDFScanError:
            raise
        except Exception:
            raise PDFScanError("PDF text extraction failed: unreadable or damaged PDF") from None
    executable = shutil.which("pdftotext")
    if executable is None:
        raise PDFScanError("PDF scanner unavailable: install pypdf or install Poppler pdftotext and add it to PATH")
    try:
        completed = subprocess.run(
            [executable, "-enc", "UTF-8", "-layout", str(path.resolve()), "-"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        raise PDFScanError("PDF text extraction could not complete") from None
    if completed.returncode != 0:
        raise PDFScanError("PDF text extraction failed: unreadable, damaged or encrypted PDF")
    try:
        text = completed.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise PDFScanError("PDF extractor returned invalid UTF-8") from None
    if not text.strip():
        raise PDFScanError("PDF has no extractable text; OCR or manual security review is required")
    return text


def check_release(root=ROOT):
    root = Path(root)
    files = distributable_files(root)
    findings = []
    pdf_scanned = 0
    for relative in files:
        path = root / relative
        if path.suffix.lower() == ".pdf":
            try:
                findings.extend(secret_findings(pdf_text(path), relative))
                pdf_scanned += 1
            except PDFScanError as error:
                findings.append({"path": relative, "rule": "pdf_scan_failed", "message": str(error)})
            continue
        # Binary artifacts are not searched as text; review them separately.
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".ico", ".ttf"}:
            continue
        findings.extend(secret_findings(path.read_text(encoding="utf-8"), relative))
    for name in TRACES:
        relative = "traces/" + name
        if relative not in files:
            findings.append({"path": relative, "rule": "missing_trace"})
            continue
        for number, line in enumerate((root / relative).read_text(encoding="utf-8").splitlines(), 1):
            event = json.loads(line)
            if (event.get("usage_source") != "simulation"
                    or event.get("source") != "isolated_integration"
                    or any(k in event for k in ("content", "arguments", "api_key", "approval_id", "description"))):
                findings.append({"path": relative, "line": number, "rule": "unsafe_trace"})
    return {"files_scanned": len(files), "pdf_files_scanned": pdf_scanned, "findings": findings,
            "scope": "Git-distributable UTF-8 and extracted PDF text, pattern scan; other binary artifacts excluded"}


if __name__ == "__main__":
    report = check_release()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(bool(report["findings"]))
