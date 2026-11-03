"""Read-only OSV queries for the captured public PyPI inventory; no API keys."""
import json
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
packages = json.loads((HERE / "installed_packages.json").read_text())
context = ssl.create_default_context()
report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
          "source": "https://api.osv.dev/v1/querybatch", "package_count": len(packages),
          "query_status": "incomplete", "matches": [], "errors": []}
try:
    data = json.dumps({"queries": [{"package": {"name": p["name"], "ecosystem": "PyPI"},
                                    "version": p["version"]} for p in packages]}).encode()
    request = Request(report["source"], data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, context=context, timeout=25) as response:
        answers = json.load(response)["results"]
    for package, answer in zip(packages, answers, strict=True):
        for vuln in answer.get("vulns", []):
            with urlopen("https://api.osv.dev/v1/vulns/" + vuln["id"], context=context, timeout=20) as response:
                advisory = json.load(response)
            report["matches"].append({"package": package, "id": vuln["id"], "advisory": advisory})
    report["query_status"] = "complete"
except Exception as exc:
    report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
(HERE / "dependency_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps({"packages": len(packages), "status": report["query_status"],
                  "advisories": [{"package": m["package"], "id": m["id"],
                                   "severity": m["advisory"].get("severity"),
                                   "summary": m["advisory"].get("summary")} for m in report["matches"]],
                  "errors": report["errors"]}, ensure_ascii=True))
