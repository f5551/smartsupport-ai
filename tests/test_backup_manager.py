import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

import backup_manager
import database
from backup_manager import BackupError, RuntimeActiveError


def _make_db(path, value):
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("CREATE TABLE sample(value TEXT NOT NULL)")
        con.execute("INSERT INTO sample VALUES (?)", (value,))
        con.execute("PRAGMA user_version=7")


def _value(path):
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT value FROM sample").fetchone()[0]


def test_backup_manifest_hashes_and_sqlite_api_roundtrip(tmp_path):
    first, second = tmp_path / "store.sqlite3", tmp_path / "auth.sqlite3"
    _make_db(first, "before-store")
    _make_db(second, "before-auth")
    lock = tmp_path / "storage.lock"
    snapshot = backup_manager.create_backup({"store": first, "auth": second}, tmp_path / "backups", lock_path=lock)

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["consistency"] == "per_database_only; database_set_is_not_atomic"
    for name, path in (("store", first), ("auth", second)):
        item = manifest["databases"][name]
        assert hashlib.sha256((snapshot / item["file"]).read_bytes()).hexdigest() == item["sha256"]
        assert item["user_version"] == 7
        with closing(sqlite3.connect(snapshot / item["file"])) as con:
            assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    with closing(sqlite3.connect(first)) as con, con:
        con.execute("UPDATE sample SET value='after-store'")
    with closing(sqlite3.connect(second)) as con, con:
        con.execute("UPDATE sample SET value='after-auth'")
    backup_manager.restore_backup(snapshot, {"store": first, "auth": second}, lock_path=lock)
    assert (_value(first), _value(second)) == ("before-store", "before-auth")


def test_corrupt_backup_is_rejected_before_any_restore(tmp_path):
    first, second = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _make_db(first, "source-one")
    _make_db(second, "source-two")
    snapshot = backup_manager.create_backup({"one": first, "two": second}, tmp_path / "backups",
                                            lock_path=tmp_path / "lock")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    image = snapshot / manifest["databases"]["two"]["file"]
    image.write_bytes(image.read_bytes() + b"corruption")
    with closing(sqlite3.connect(first)) as con, con:
        con.execute("UPDATE sample SET value='unchanged-target'")
    with pytest.raises(BackupError, match="checksum"):
        backup_manager.restore_backup(snapshot, {"one": first, "two": second}, lock_path=tmp_path / "lock")
    assert _value(first) == "unchanged-target"


def test_rehashed_non_sqlite_image_fails_integrity_validation(tmp_path):
    source = tmp_path / "source.sqlite3"
    _make_db(source, "source")
    snapshot = backup_manager.create_backup({"store": source}, tmp_path / "backups",
                                            lock_path=tmp_path / "lock")
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = snapshot / manifest["databases"]["store"]["file"]
    image.write_bytes(b"not an sqlite database")
    manifest["databases"]["store"]["sha256"] = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest["databases"]["store"]["size_bytes"] = image.stat().st_size
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    manifest_path.write_bytes(payload)
    (snapshot / "manifest.sha256").write_text(hashlib.sha256(payload).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(BackupError, match="integrity check"):
        backup_manager.restore_backup(snapshot, {"store": source}, lock_path=tmp_path / "lock")


def test_interrupted_backup_does_not_publish_partial_set(tmp_path, monkeypatch):
    first, second = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _make_db(first, "one")
    _make_db(second, "two")
    destination = tmp_path / "backups"
    original = backup_manager._sqlite_backup
    calls = 0

    def interrupt(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BackupError("simulated interruption")
        original(source, target)

    monkeypatch.setattr(backup_manager, "_sqlite_backup", interrupt)
    with pytest.raises(BackupError, match="interruption"):
        backup_manager.create_backup({"one": first, "two": second}, destination, lock_path=tmp_path / "lock")
    assert list(destination.iterdir()) == []


def test_interrupted_multi_database_restore_rolls_back_prior_file(tmp_path, monkeypatch):
    first, second = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _make_db(first, "backup-one")
    _make_db(second, "backup-two")
    snapshot = backup_manager.create_backup({"one": first, "two": second}, tmp_path / "backups",
                                            lock_path=tmp_path / "lock")
    with closing(sqlite3.connect(first)) as con, con:
        con.execute("UPDATE sample SET value='live-one'")
    with closing(sqlite3.connect(second)) as con, con:
        con.execute("UPDATE sample SET value='live-two'")
    original_replace = backup_manager.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated restore interruption")
        return original_replace(source, target)

    monkeypatch.setattr(backup_manager.os, "replace", fail_second)
    with pytest.raises(BackupError, match="rolled back"):
        backup_manager.restore_backup(snapshot, {"one": first, "two": second}, lock_path=tmp_path / "lock")
    assert (_value(first), _value(second)) == ("live-one", "live-two")


def test_restore_refuses_while_runtime_lease_is_held(tmp_path):
    target = tmp_path / "target.sqlite3"
    _make_db(target, "live-value")
    lock = tmp_path / "lock"
    snapshot = backup_manager.create_backup({"store": target}, tmp_path / "backups", lock_path=lock)
    with backup_manager.runtime_lease(lock):
        with pytest.raises(RuntimeActiveError, match="Storage is active"):
            backup_manager.restore_backup(snapshot, {"store": target}, lock_path=lock)
    assert _value(target) == "live-value"


def test_web_backend_holds_application_lease_until_closed(tmp_path, monkeypatch):
    from web_runtime import WebBackend

    source = tmp_path / "source.sqlite3"
    _make_db(source, "saved")
    snapshot = backup_manager.create_backup({"store": source}, tmp_path / "backups",
                                            lock_path=tmp_path / "service.lock")
    monkeypatch.setenv("SMARTSUPPORT_STORAGE_LOCK_PATH", str(tmp_path / "service.lock"))
    backend = WebBackend(tmp_path / "web")
    try:
        with pytest.raises(RuntimeActiveError):
            backup_manager.restore_backup(snapshot, {"store": source}, lock_path=tmp_path / "service.lock")
    finally:
        backend.close()
    backup_manager.restore_backup(snapshot, {"store": source}, lock_path=tmp_path / "service.lock")


def test_process_crash_during_restore_blocks_startup_and_preserves_recovery(tmp_path):
    first, second = tmp_path / "one.sqlite3", tmp_path / "two.sqlite3"
    _make_db(first, "backup-one")
    _make_db(second, "backup-two")
    lock = tmp_path / "lock"
    paths = {"one": first, "two": second}
    snapshot = backup_manager.create_backup(paths, tmp_path / "backups", lock_path=lock)
    for path, value in ((first, "live-one"), (second, "live-two")):
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("UPDATE sample SET value=?", (value,))
    code = '''
import os, sys
from pathlib import Path
import backup_manager as bm
original = bm.os.replace
def crash_after_first(source, target):
    original(source, target)
    os._exit(71)
bm.os.replace = crash_after_first
bm.restore_backup(sys.argv[1], {"one": Path(sys.argv[2]), "two": Path(sys.argv[3])}, lock_path=sys.argv[4])
'''
    result = subprocess.run([sys.executable, "-c", code, str(snapshot), str(first), str(second), str(lock)],
                            cwd=backup_manager.ROOT, capture_output=True, timeout=20)
    assert result.returncode == 71
    assert (_value(first), _value(second)) == ("backup-one", "live-two")
    with pytest.raises(BackupError, match="Interrupted restore"):
        backup_manager.acquire_runtime_lease(lock)
    marker = backup_manager._restore_marker(lock)
    recovery = json.loads(marker.read_text(encoding="utf-8"))
    assert all(__import__("pathlib").Path(path).is_file() for path in recovery["rollback"].values())


def test_restored_write_idempotency_record_prevents_duplicate_ticket(tmp_path):
    path = tmp_path / "store.sqlite3"
    database.init_database(path)
    database.seed_demo_data(path)
    key = "a" * 64
    prior = database.create_ticket("demo_customer_001", "DEMO-1002", "battery issue",
                                  approved=True, db_path=path, idempotency_key=key,
                                  session_id="backup-test-session")
    snapshot = backup_manager.create_backup({"store": path}, tmp_path / "backups",
                                            lock_path=tmp_path / "lock")
    database.create_ticket("demo_customer_001", "DEMO-1002", "another issue",
                           approved=True, db_path=path, idempotency_key="b" * 64,
                           session_id="backup-test-session")

    backup_manager.restore_backup(snapshot, {"store": path}, lock_path=tmp_path / "lock")
    replay = database.create_ticket("demo_customer_001", "DEMO-1002", "battery issue",
                                    approved=True, db_path=path, idempotency_key=key,
                                    session_id="backup-test-session")
    assert replay == prior
    with database.connect(path) as con:
        assert con.execute("SELECT count(*) FROM tickets").fetchone()[0] == 4
        assert con.execute("SELECT count(*) FROM write_operations").fetchone()[0] == 1


def test_discovery_only_returns_existing_known_databases(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "missing-store.sqlite3"))
    monkeypatch.setenv("DAILY_COST_DB_PATH", str(tmp_path / "missing-cost.sqlite3"))
    web = tmp_path / "web"
    tenant = web / "synthetic-tenant"
    tenant.mkdir(parents=True)
    (tenant / "store.sqlite3").touch()
    (tenant / "approvals.sqlite3").touch()
    found = backup_manager.discover_database_paths(web_data_dir=web,
                                                   extra_paths={"test_extra": tmp_path / "missing.sqlite3"})
    assert len(found) == 2
    assert all("synthetic-tenant" not in name for name in found)
    assert {name.rsplit("_", 1)[-1] for name in found} == {"store", "approvals"}
