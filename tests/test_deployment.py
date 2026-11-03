"""Deployment preflight: isolated stores and actual subprocess protocols, no API."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from streamlit.testing.v1 import AppTest

from config import ROOT
from database import init_database, seed_demo_data, connect, get_order_status
from release_check import check_release, distributable_files, secret_findings
from runtime_policy import public_demo_only, web_data_directory, worker_limit
from web_controls import WebError
from web_runtime import WebBackend, AgentWorker
from mcp_transport import TrustedSession
from mcp_client_test import smoke_test
from web_ui import get_web_backend


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("SMARTSUPPORT_DEPLOYMENT", "local")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("web_runtime.AsyncOpenAI", lambda **kwargs: pytest.fail("Paid client forbidden"))


def test_render_overrides_local_flags_and_rejects_direct_live(public, tmp_path):
    service = WebBackend(tmp_path, bind_address="127.0.0.1")
    try:
        assert public_demo_only() and not service.live_allowed()
        visitor = service.new_visitor()
        with pytest.raises(WebError, match="live_disabled"):
            service.new_conversation(visitor, "عميل تجريبي 1", "Live")
        with pytest.raises(WebError):
            AgentWorker(TrustedSession("demo_customer_001", "test-session"), tmp_path, "Live")
    finally:
        service.close()


def test_public_ui_ignores_live_and_identity_query_parameters(public, tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_WEB_DATA_DIR", str(tmp_path))
    get_web_backend.clear()
    try:
        ui = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        ui.query_params["mode"] = "Live"
        ui.query_params["customer_id"] = "arbitrary"
        ui.run()
        assert not ui.exception
        assert ui.selectbox(key="mode").options == ["Demo"]
        assert len(ui.selectbox(key="customer").options) == 5
    finally:
        get_web_backend.clear()


def test_public_default_root_is_temporary_not_developer_database(public, monkeypatch):
    monkeypatch.delenv("SMARTSUPPORT_WEB_DATA_DIR", raising=False)
    assert web_data_directory() != ROOT / "data" / "web_demo"
    assert ROOT not in web_data_directory().parents


def test_relative_operator_root_does_not_depend_on_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SMARTSUPPORT_WEB_DATA_DIR", "data/web_demo")
    assert web_data_directory() == ROOT / "data" / "web_demo"


def test_explicit_public_lock_without_render_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", "1")
    monkeypatch.setenv("SMARTSUPPORT_DEPLOYMENT", "local")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    service = WebBackend(tmp_path)
    try:
        assert not service.live_allowed()
    finally:
        service.close()


@pytest.mark.parametrize("value,expected", [("2", 2), ("100", 4), ("0", 1), ("bad", 1)])
def test_worker_limit_bounded(value, expected, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_MAX_WORKERS", value)
    assert worker_limit() == expected


def test_concurrent_public_visitors_writes_and_memory_are_isolated(public, tmp_path):
    service = WebBackend(tmp_path)
    def exercise(marker):
        visitor = service.new_visitor()
        identifier = service.new_conversation(visitor, "عميل تجريبي 1")
        pending = service.send(visitor, identifier, "إنشاء شكوى للطلب DEMO-1002")
        assert pending["stop_reason"] == "pending_approval"
        result = service.decide(visitor, identifier, pending["pending_approval"]["pending_id"], "approve")
        assert result["tools_used"] == ["create_ticket"]
        pending = service.send(visitor, identifier, "احفظ هذا الملخص بعد موافقتي: " + marker)
        service.decide(visitor, identifier, pending["pending_approval"]["pending_id"], "approve")
        return visitor, identifier
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(exercise, ["زائر ألف", "زائر باء"]))
        for own, other in [(first, second), (second, first)]:
            with pytest.raises(WebError, match="session_lost"):
                service.view(own[0], other[1])
        for pair, marker, forbidden in [(first, "زائر ألف", "زائر باء"), (second, "زائر باء", "زائر ألف")]:
            directory = service.conversations[pair[1]].directory
            with sqlite3.connect(directory / "store.sqlite3") as db:
                assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 4
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                memories = str(db.execute("SELECT content FROM conversation_memory").fetchall())
                assert marker in memories and forbidden not in memories
        assert service.conversations[first[1]].directory != service.conversations[second[1]].directory
    finally:
        service.close()


def test_fresh_initialization_is_not_repeated_on_views(public, tmp_path, monkeypatch):
    service = WebBackend(tmp_path)
    try:
        visitor = service.new_visitor()
        identifier = service.new_conversation(visitor, "عميل تجريبي 1")
        monkeypatch.setattr("web_runtime.seed_demo_data", lambda *_: pytest.fail("Reseed on rerun"))
        for _ in range(3):
            assert service.view(visitor, identifier)["mode"] == "Demo"
        assert service.send(visitor, identifier, "ما حالة الطلب DEMO-2001؟")["tools_used"] == ["get_order_status"]
    finally:
        service.close()
    replacement = WebBackend(tmp_path)
    try:
        with pytest.raises(WebError, match="session_lost"):
            replacement.view(visitor, identifier)
    finally:
        replacement.close()


def test_sqlite_uri_special_characters_and_imports_from_other_cwd(tmp_path):
    path = tmp_path / "متجر space # question?" / "demo.sqlite3"
    # '?' is a legal Linux name but not a Windows name.
    if os.name == "nt":
        path = tmp_path / "متجر space # percent%" / "demo.sqlite3"
    init_database(path)
    seed_demo_data(path)
    seed_demo_data(path)
    assert get_order_status("demo_customer_001", "DEMO-1001", db_path=path)["status"] == "shipped"
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 10
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-c", "import agent, mcp_server, mcp_transport, database, runtime_policy"],
                            cwd=tmp_path, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    assert asyncio.run(smoke_test(path))["transport"] == "stdio subprocess"


@pytest.mark.skipif(sys.platform != "linux", reason="Requires a real Linux runner")
def test_linux_private_runtime_permissions(tmp_path):
    root = tmp_path / "private"
    service = WebBackend(root)
    try:
        assert root.stat().st_mode & 0o077 == 0
    finally:
        service.close()


def test_streamlit_fresh_process_health_and_no_private_file_serving(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "RENDER": "true", "SMARTSUPPORT_WEB_DATA_DIR": str(tmp_path / "runtime"),
           "RUN_LIVE_OPENAI": "0", "PYTHONIOENCODING": "utf-8"}
    env.pop("OPENAI_API_KEY", None)
    log = tmp_path / "startup.log"
    with log.open("wb") as out:
        process = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py",
                                    "--server.address", "127.0.0.1", "--server.port", str(port),
                                    "--server.headless", "true"], cwd=ROOT, env=env, stdout=out, stderr=out)
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 35
            while True:
                try:
                    with urllib.request.urlopen(base + "/_stcore/health", timeout=2) as response:
                        assert response.status == 200 and response.read() == b"ok"
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline or process.poll() is not None:
                        pytest.fail("Streamlit failed to become healthy")
                    time.sleep(.2)
            for route in ["/.env", "/data/seed_orders.json", "/app/static/data/smartsupport.db", "/mcp_server.py"]:
                try:
                    with urllib.request.urlopen(base + route, timeout=3) as response:
                        body = response.read()
                except urllib.error.HTTPError as error:
                    assert error.code in {403, 404}
                    continue
                # SPA fallback is permissible; source/data downloads are not.
                assert b"SQLite format 3" not in body and b"demo_customer_001" not in body
                assert b"OPENAI_API_KEY=" not in body and b"from mcp.server" not in body
        finally:
            process.terminate()
            process.wait(timeout=15)


def test_gitignore_excludes_runtime_and_preserves_only_reviewed_traces(tmp_path):
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    paths = [".env", ".env.example", ".envrc", ".venv/secret", "a/__pycache__/x.pyc", ".pytest_cache/x",
             "data/private.sqlite3", "data/private.sqlite3-wal", "audit.jsonl", ".streamlit/secrets.toml",
             "secret.pem", "secret.p12", "secret.pfx", "cache.tmp", "backup.bak", "smartsupport-ai.zip",
             "traces/unreviewed.jsonl", "traces/success_1.jsonl", "app.py"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    assert set(distributable_files(tmp_path)) == {".gitignore", ".env.example", "traces/success_1.jsonl", "app.py"}


def test_secret_scan_detects_unknown_key_without_disclosing_it():
    key = "sk-proj-" + "A" * 64
    findings = secret_findings("key=" + key, "tests/unknown.py")
    assert findings == [{"path": "tests/unknown.py", "line": 1, "rule": "openai_key"}]
    assert key not in json.dumps(findings)


def test_release_text_and_capstone_traces_are_clean():
    assert check_release()["findings"] == []


def test_blueprint_commands_and_python_pin():
    text = (ROOT / "render.yaml").read_text()
    assert "runtime: python" in text and "plan: free" in text
    assert "buildCommand: pip install -r requirements.txt" in text
    assert "startCommand: python -m streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true" in text
    assert "healthCheckPath: /_stcore/health" in text
    assert "OPENAI_API_KEY" not in text and "rootDir:" not in text
    assert (ROOT / ".python-version").read_text().strip() == "3.13.7"
