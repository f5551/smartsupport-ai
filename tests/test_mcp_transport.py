"""Regression tests for the MCP subprocess environment boundary."""

import mcp_transport


def test_mcp_child_receives_custom_storage_lock_without_credentials(monkeypatch, tmp_path):
    lock_path = str(tmp_path / "storage.lock")
    monkeypatch.setenv("SMARTSUPPORT_STORAGE_LOCK_PATH", lock_path)
    fake_key = "sk-" + "test-value-must-not-cross-boundary"
    monkeypatch.setenv("OPENAI_API_KEY", fake_key)

    environment = mcp_transport._child_environment()

    assert environment["SMARTSUPPORT_STORAGE_LOCK_PATH"] == lock_path
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert "OPENAI_API_KEY" not in environment
