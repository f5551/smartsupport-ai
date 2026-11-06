"""Offline SQLite backup sets with integrity verification and maintenance locking.

Each database is copied with SQLite's online Backup API. A set of multiple
files is not one atomic snapshot; callers must include every relevant database
and keep the application offline for restore. Application runtimes hold a
shared OS lock; backup/restore take it exclusively and fail while a runtime is
active.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from collections.abc import Mapping
from datetime import datetime, timezone
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from uuid import uuid4

import database
from daily_cost import DEFAULT_PATH as DEFAULT_DAILY_COST_PATH

ROOT = Path(__file__).resolve().parent
FORMAT_VERSION = 1
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")


class BackupError(RuntimeError):
    """Safe, non-sensitive backup or restore error."""


class RuntimeActiveError(BackupError):
    """An application or MCP runtime still owns the storage lock."""


def maintenance_lock_path() -> Path:
    value = _setting("SMARTSUPPORT_STORAGE_LOCK_PATH")
    return Path(value).expanduser().resolve() if value else (ROOT / ".runtime" / "storage-maintenance.lock")


def _setting(key: str) -> str | None:
    """Read only a non-secret path setting; never materialize unrelated .env values."""
    value = os.environ.get(key)
    if value:
        return value
    env_file = ROOT / ".env"
    try:
        with env_file.open("r", encoding="utf-8") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name, candidate = stripped.split("=", 1)
                if name.strip() == key:
                    candidate = candidate.strip()
                    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "\"'":
                        candidate = candidate[1:-1]
                    return candidate or None
    except OSError:
        return None
    return None


class _FileLock:
    def __init__(self, path: Path, *, exclusive: bool, blocking: bool):
        self.path = path
        self.exclusive = exclusive
        self.blocking = blocking
        self.file = None
        self._overlapped = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, os.SEEK_END)
                if self.file.tell() == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                class OVERLAPPED(ctypes.Structure):
                    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                                ("hEvent", ctypes.c_void_p)]
                self._overlapped = OVERLAPPED()
                flags = 0x00000001 if not self.blocking else 0  # FAIL_IMMEDIATELY
                if self.exclusive:
                    flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
                handle = msvcrt.get_osfhandle(self.file.fileno())
                ok = ctypes.windll.kernel32.LockFileEx(
                    ctypes.c_void_p(handle), ctypes.c_uint32(flags), ctypes.c_uint32(0),
                    ctypes.c_uint32(1), ctypes.c_uint32(0), ctypes.byref(self._overlapped))
                if not ok:
                    raise RuntimeActiveError("Storage is active; stop the application before backup or restore")
            else:
                import fcntl
                op = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                if not self.blocking:
                    op |= fcntl.LOCK_NB
                try:
                    fcntl.flock(self.file.fileno(), op)
                except BlockingIOError as exc:
                    raise RuntimeActiveError("Storage is active; stop the application before backup or restore") from exc
            return self
        except BaseException:
            self.file.close()
            self.file = None
            raise

    def close(self):
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle = msvcrt.get_osfhandle(self.file.fileno())
                ctypes.windll.kernel32.UnlockFileEx(
                    ctypes.c_void_p(handle), ctypes.c_uint32(0), ctypes.c_uint32(1),
                    ctypes.c_uint32(0), ctypes.byref(self._overlapped))
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


@contextmanager
def runtime_lease(lock_path: str | Path | None = None):
    """Hold a shared lock for the lifetime of an application/MCP runtime."""
    lease = acquire_runtime_lease(lock_path)
    try:
        yield
    finally:
        lease.close()


def acquire_runtime_lease(lock_path: str | Path | None = None):
    """Acquire a shared lease for an application object until explicitly closed."""
    path = Path(lock_path or maintenance_lock_path())
    lease = _FileLock(path, exclusive=False, blocking=True).acquire()
    if _restore_marker(path).exists():
        lease.close()
        raise BackupError("Interrupted restore requires operator reconciliation before startup")
    return lease


def _restore_marker(lock_path):
    return Path(str(lock_path or maintenance_lock_path()) + ".restore-pending")


def _maintenance_lock(lock_path: str | Path | None):
    lease = _FileLock(Path(lock_path or maintenance_lock_path()), exclusive=True, blocking=False).acquire()
    if _restore_marker(lock_path).exists():
        lease.close()
        raise BackupError("Interrupted restore requires operator reconciliation before maintenance")
    return lease


def discover_database_paths(*, web_data_dir: str | Path | None = None,
                            extra_paths: dict[str, str | Path] | None = None) -> dict[str, Path]:
    """Discover this application's known SQLite files without creating them."""
    store = Path(_setting("SQLITE_PATH") or database.DEFAULT_DB_PATH)
    if not store.is_absolute():
        store = ROOT / store
    daily = Path(_setting("DAILY_COST_DB_PATH") or DEFAULT_DAILY_COST_PATH)
    if not daily.is_absolute():
        daily = ROOT / daily
    if web_data_dir is None:
        configured = _setting("SMARTSUPPORT_WEB_DATA_DIR")
        web_root = Path(configured).expanduser() if configured else ROOT / "data" / "web_demo"
        if not web_root.is_absolute():
            web_root = ROOT / web_root
    else:
        web_root = Path(web_data_dir).expanduser()

    candidates: dict[str, Path] = {"store_default": store, "daily_cost": daily}
    for name in ("web_sessions.sqlite3", "web_controls.sqlite3", "auth.sqlite3"):
        candidates[f"web_{name.removesuffix('.sqlite3')}"] = web_root / name
    for child in sorted(web_root.iterdir()) if web_root.exists() else ():
        if child.is_dir():
            # Directory identifiers may correlate to a customer/session. Never
            # copy their raw value into the manifest or backup file name.
            tenant_tag = hashlib.sha256(child.name.encode("utf-8")).hexdigest()[:16]
            for name in ("store.sqlite3", "approvals.sqlite3"):
                candidates[f"tenant_{tenant_tag}_{name.removesuffix('.sqlite3')}"] = child / name
    for key, value in (extra_paths or {}).items():
        candidates[key] = Path(value)

    result = {}
    seen = set()
    for name, path in candidates.items():
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError("Invalid database name")
        resolved = path.expanduser().resolve()
        if resolved.is_file() and resolved not in seen:
            result[name] = resolved
            seen.add(resolved)
    return result


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _integrity(path: Path) -> None:
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as con:
            rows = con.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            raise BackupError("SQLite integrity check failed")
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise BackupError("Database could not be opened for integrity check") from exc


def _sqlite_backup(source: Path, destination: Path) -> None:
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as src:
            with closing(sqlite3.connect(destination, timeout=10)) as dst:
                src.backup(dst, pages=256, sleep=0.02)
                dst.commit()
    except sqlite3.Error as exc:
        raise BackupError("SQLite backup operation failed") from exc
    if os.name != "nt":
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass


def create_backup(database_paths: Mapping[str, str | Path], backup_root: str | Path,
                  *, lock_path: str | Path | None = None) -> Path:
    """Create a private backup set; manifest is written last as its commit marker."""
    if not database_paths:
        raise BackupError("No databases were supplied")
    normalized = {}
    for name, value in database_paths.items():
        if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
            raise BackupError("Invalid database name")
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise BackupError("A requested database is missing")
        normalized[name] = path
    if len(set(normalized.values())) != len(normalized):
        raise BackupError("A database path was listed more than once")

    root = Path(backup_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
    lease = _maintenance_lock(lock_path)
    final = root / f"backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    try:
        final.mkdir(mode=0o700)
        items = {}
        for name, source in sorted(normalized.items()):
            _integrity(source)
            filename = f"{name}.sqlite3"
            target = final / filename
            _sqlite_backup(source, target)
            _integrity(target)
            items[name] = {"file": filename, "sha256": _digest(target),
                           "size_bytes": target.stat().st_size,
                           "user_version": _user_version(target),
                           "target_path": str(source)}
        manifest = {"format_version": FORMAT_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "consistency": "per_database_only; database_set_is_not_atomic",
                    "databases": items}
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        (final / "manifest.sha256").write_text(hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii")
        (final / "manifest.json").write_bytes(payload)
        if os.name != "nt":
            os.chmod(final / "manifest.json", 0o600)
            os.chmod(final / "manifest.sha256", 0o600)
        return final
    except BaseException:
        shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        lease.close()


def _user_version(path: Path) -> int:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as con:
        return int(con.execute("PRAGMA user_version").fetchone()[0])


def _load_verified_manifest(backup_dir: Path) -> dict:
    try:
        raw = (backup_dir / "manifest.json").read_bytes()
        expected = (backup_dir / "manifest.sha256").read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or hashlib.sha256(raw).hexdigest() != expected:
            raise BackupError("Backup manifest checksum mismatch")
        manifest = json.loads(raw)
        if manifest.get("format_version") != FORMAT_VERSION or not isinstance(manifest.get("databases"), dict):
            raise BackupError("Unsupported backup manifest")
        for name, item in manifest["databases"].items():
            if not _SAFE_NAME.fullmatch(name) or not isinstance(item, dict):
                raise BackupError("Invalid backup manifest entry")
            filename = item.get("file")
            if filename != f"{name}.sqlite3":
                raise BackupError("Invalid backup file name")
            target_path = item.get("target_path")
            if not isinstance(target_path, str) or not Path(target_path).is_absolute():
                raise BackupError("Invalid restore target in manifest")
            image = backup_dir / filename
            if not image.is_file() or _digest(image) != item.get("sha256"):
                raise BackupError("Backup database checksum mismatch")
            if image.stat().st_size != item.get("size_bytes"):
                raise BackupError("Backup database size mismatch")
            _integrity(image)
            if _user_version(image) != item.get("user_version"):
                raise BackupError("Backup database metadata mismatch")
        return manifest
    except BackupError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise BackupError("Backup set is invalid or incomplete") from exc


def backup_targets(backup_dir: str | Path) -> dict[str, Path]:
    """Return the original destinations after validating the complete snapshot."""
    manifest = _load_verified_manifest(Path(backup_dir).expanduser().resolve())
    return {name: Path(item["target_path"]).resolve()
            for name, item in manifest["databases"].items()}


def restore_backup(backup_dir: str | Path, database_paths: Mapping[str, str | Path],
                   *, lock_path: str | Path | None = None) -> None:
    """Restore a whole set after verifying it; refuses while a runtime is active.

    Replacement is atomic per database file, not across the database set. If a
    later replacement fails, already replaced files are rolled back best-effort.
    """
    source_dir = Path(backup_dir).expanduser().resolve()
    manifest = _load_verified_manifest(source_dir)
    targets = {key: Path(value).expanduser().resolve() for key, value in database_paths.items()}
    if set(targets) != set(manifest["databases"]):
        raise BackupError("Restore target set does not match the backup")
    if len(set(targets.values())) != len(targets):
        raise BackupError("A restore target was listed more than once")
    for name, target in targets.items():
        recorded = Path(manifest["databases"][name]["target_path"]).resolve()
        if target != recorded:
            raise BackupError("Restore target does not match the backup manifest")
    lease = _maintenance_lock(lock_path)
    staging: dict[str, Path] = {}
    rollback: dict[str, Path | None] = {}
    replaced = []
    marker = _restore_marker(lock_path)
    preserve_recovery = False
    try:
        for name, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if Path(str(target) + "-wal").exists() or Path(str(target) + "-shm").exists():
                raise BackupError("SQLite sidecar files exist; stop all database users before restore")
            handle, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.restore-", suffix=".sqlite3", dir=target.parent)
            os.close(handle)
            candidate = Path(tmp_name)
            _sqlite_backup(source_dir / manifest["databases"][name]["file"], candidate)
            _integrity(candidate)
            staging[name] = candidate
            if target.exists():
                handle, old_name = tempfile.mkstemp(prefix=f".{target.name}.rollback-", suffix=".sqlite3", dir=target.parent)
                os.close(handle)
                old = Path(old_name)
                _sqlite_backup(target, old)
                _integrity(old)
                rollback[name] = old
            else:
                rollback[name] = None
        # Durable fence: a hard crash between replacements must block startup.
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"targets": {name: str(path) for name, path in targets.items()},
                       "rollback": {name: str(path) if path else None for name, path in rollback.items()}}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        for name, target in targets.items():
            os.replace(staging[name], target)
            replaced.append(name)
        for target in targets.values():
            _integrity(target)
        marker.unlink()
    except BaseException as exc:
        rollback_errors = []
        for name in reversed(replaced):
            target = targets[name]
            previous = rollback.get(name)
            try:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(previous, target)
                    _integrity(target)
            except Exception:
                rollback_errors.append(name)
        if rollback_errors:
            preserve_recovery = True
            raise BackupError("Restore failed and rollback needs operator attention") from exc
        marker.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("Restore failed; original databases were preserved or rolled back") from exc
    finally:
        for path in (() if preserve_recovery else (*staging.values(), *(p for p in rollback.values() if p))):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        lease.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline SmartSupport SQLite backup/restore")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--create", metavar="BACKUP_ROOT", help="create a verified backup set")
    actions.add_argument("--restore", metavar="BACKUP_SET", help="restore a verified backup set")
    parser.add_argument("--confirm-restore", action="store_true",
                        help="required with --restore because restore replaces live database files")
    parser.add_argument("--web-data-dir", help="web runtime data directory (defaults to configured local path)")
    parser.add_argument("--database", action="append", default=[], metavar="NAME=PATH",
                        help="additional SQLite database to include; repeat as needed")
    args = parser.parse_args(argv)
    if args.restore and not args.confirm_restore:
        parser.error("--restore requires --confirm-restore")
    if args.confirm_restore and not args.restore:
        parser.error("--confirm-restore is only valid with --restore")
    try:
        if args.create:
            paths = discover_database_paths(web_data_dir=args.web_data_dir)
            for entry in args.database:
                name, separator, value = entry.partition("=")
                if not separator or not name or not value:
                    parser.error("--database must be NAME=PATH")
                paths[name] = Path(value).expanduser()
            result = create_backup(paths, args.create)
            print(f"Backup created: {result}")
        else:
            targets = backup_targets(args.restore)
            restore_backup(args.restore, targets)
            print("Backup restored and verified; per-database only, not an atomic set")
        return 0
    except (BackupError, OSError, ValueError) as exc:
        print(f"Backup operation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
