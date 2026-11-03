import json

import pytest

from audit_logger import AuditError, AuditLogger


def test_jsonl_required_fields_and_secret_allowlist(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLogger(path, source="unit_test")
    secrets = ["sk-this-is-a-test-secret-not-a-real-key", "person@example.invalid", "approval-private-reference", "+966500000000", "رسالة عميل خاصة"]
    audit.emit(task_id="task", session_id=secrets[1], event_type="task_start", status="started",
               api_key=secrets[0], approval_id=secrets[2], customer_id=secrets[3], message=secrets[4],
               payload={"password": "not-a-real-password"}, input_tokens=20, estimated_cost_usd="0.001")
    raw = path.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert all(s not in raw for s in secrets)
    assert "not-a-real-password" not in raw
    assert event["session_id"].startswith("session_")
    assert {"timestamp", "task_id", "session_id", "event_type", "tool_name", "status", "duration_ms", "input_tokens", "output_tokens", "estimated_cost_usd", "stop_reason", "error_code"} <= event.keys()
    assert event["input_tokens"] == 20


def test_unknown_fields_and_untrusted_event_labels_cannot_leak_text(tmp_path):
    log = AuditLogger(tmp_path / "events.jsonl")
    event = log.emit(task_id="x", session_id="s", event_type="security_event", status="blocked",
        tool_name="secret@example.invalid", error_code="password=private", stop_reason="secret",
        estimated_cost_usd="sk-secret", input_tokens=True)
    assert event["tool_name"] is None and event["error_code"] is None
    assert event["estimated_cost_usd"] is None and event["input_tokens"] is None
    with pytest.raises(ValueError):
        log.emit(task_id="x", session_id="s", event_type="raw secret", status="failure")


def test_audit_write_failure_is_explicit(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    log = AuditLogger(directory)
    with pytest.raises(AuditError):
        log.emit(task_id="x", session_id="s", event_type="task_start", status="started")
    assert log.events == []
