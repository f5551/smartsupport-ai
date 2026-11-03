from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from web_controls import WebControls, WebError
from web_runtime import WebBackend, CUSTOMERS


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.delenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", raising=False)
    service = WebBackend(tmp_path)
    yield service
    service.close()


def chat(backend, label="عميل تجريبي 1"):
    visitor = backend.new_visitor()
    return visitor, backend.new_conversation(visitor, label)


def ticket_count(backend, identifier):
    with sqlite3.connect(backend.conversations[identifier].directory / "store.sqlite3") as con:
        return con.execute("SELECT count(*) FROM tickets").fetchone()[0]


def test_demo_reads_use_actual_mcp_and_no_paid_client(backend, monkeypatch):
    monkeypatch.setattr("web_runtime.AsyncOpenAI", lambda **kwargs: pytest.fail("Demo must not construct paid client"))
    visitor, identifier = chat(backend)
    result = backend.send(visitor, identifier, "ما مدة الشحن؟")
    assert result["stop_reason"] == "final_answer" and result["tools_used"] == ["search_faq"]
    assert "FAQ-001" in result["message"] and result["cost_summary"]["usage_source"] == "simulation"
    log = backend.conversations[identifier].directory / "audit.jsonl"
    assert any(e["event_type"] == "mcp_result" and e["status"] == "success" for e in map(json.loads, log.read_text().splitlines()))


def test_demo_customer_allowlist_and_namespace_isolation(backend):
    visitor, identifier = chat(backend)
    another = backend.new_visitor()
    with pytest.raises(WebError, match="session_lost"):
        backend.view(another, identifier)
    with pytest.raises(WebError):
        backend.new_conversation(visitor, "arbitrary_customer")
    result = backend.send(visitor, identifier, "ما حالة الطلب DEMO-2001؟")
    assert "غير متاحة" in result["message"]
    second = backend.new_conversation(visitor, "عميل تجريبي 2", previous=identifier)
    assert backend.view(visitor, second)["messages"] == []
    assert "قيد التجهيز" in backend.send(visitor, second, "ما حالة الطلب DEMO-2001؟")["message"]
    assert backend.conversations[identifier].directory != backend.conversations[second].directory


def test_pending_write_is_not_executed_and_decision_is_one_shot(backend):
    visitor, identifier = chat(backend)
    pending = backend.send(visitor, identifier, "إنشاء شكوى للطلب DEMO-1002")
    assert ticket_count(backend, identifier) == 3
    assert pending["tools_used"] == [] and pending["stop_reason"] == "pending_approval"
    nonce = pending["pending_approval"]["pending_id"]
    assert "approval_id" not in str(pending) and "call_id" not in str(pending)
    result = backend.decide(visitor, identifier, nonce, "approve")
    assert result["tools_used"] == ["create_ticket"] and "TKT-" in result["message"]
    assert ticket_count(backend, identifier) == 4
    with pytest.raises(WebError, match="already_handled"):
        backend.decide(visitor, identifier, nonce, "approve")
    assert ticket_count(backend, identifier) == 4


def test_cancellation_creates_no_ticket(backend):
    visitor, identifier = chat(backend)
    pending = backend.send(visitor, identifier, "إنشاء شكوى للطلب DEMO-1002")
    result = backend.decide(visitor, identifier, pending["pending_approval"]["pending_id"], "reject")
    assert "أُلغيت" in result["message"] and ticket_count(backend, identifier) == 3


def test_memory_requires_consent_and_survives_worker_restart(backend):
    visitor, identifier = chat(backend)
    pending = backend.send(visitor, identifier, "احفظ هذا الملخص بعد موافقتي: أفضل التواصل بالعربية")
    backend.decide(visitor, identifier, pending["pending_approval"]["pending_id"], "approve")
    con = backend.conversations[identifier]
    con.worker.close()
    con.worker = None
    result = backend.send(visitor, identifier, "استرجع الملخص المحفوظ لهذه المحادثة")
    assert "أفضل التواصل بالعربية" in result["message"] and result["tools_used"] == ["load_memory"]


def test_lost_pending_state_fails_before_grant_issuance(backend):
    visitor, identifier = chat(backend)
    pending = backend.send(visitor, identifier, "إنشاء شكوى للطلب DEMO-1002")
    con = backend.conversations[identifier]
    con.worker.close()
    with pytest.raises(WebError, match="session_lost"):
        backend.decide(visitor, identifier, pending["pending_approval"]["pending_id"], "approve")
    assert ticket_count(backend, identifier) == 3
    assert not (con.directory / "approvals.sqlite3").exists()
    assert backend.view(visitor, identifier)["result"]["stop_reason"] == "session_lost"


@pytest.mark.parametrize("message", ["", "x" * 1001, "sk-this-is-a-synthetic-secret-to-block"])
def test_backend_validates_length_and_secret_input_independently_of_widgets(backend, message):
    visitor, identifier = chat(backend)
    with pytest.raises(WebError, match="invalid_input"):
        backend.send(visitor, identifier, message)


def test_persistent_limits_survive_controls_restart(tmp_path):
    path = tmp_path / "controls.sqlite3"
    first = WebControls(path, per_session=1)
    first.consume("visitor")
    second = WebControls(path, per_session=1)
    with pytest.raises(WebError, match="rate_limit"):
        second.consume("visitor")
    first.claim_decision("conversation", "pending")
    with pytest.raises(WebError, match="already_handled"):
        second.claim_decision("conversation", "pending")


def test_concurrent_decision_claims_have_one_winner(tmp_path):
    controls = WebControls(tmp_path / "controls.sqlite3")
    def claim(_):
        try:
            controls.claim_decision("conversation", "pending")
            return True
        except WebError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, range(2))) == [False, True]


@pytest.mark.parametrize("bind,deployment,enabled,expected", [
    ("127.0.0.1", "local", "1", True), ("0.0.0.0", "local", "1", False),
    ("127.0.0.1", "public", "1", False), ("127.0.0.1", "local", "0", False),
])
def test_live_gate_requires_operator_flag_and_local_binding(tmp_path, monkeypatch, bind, deployment, enabled, expected):
    monkeypatch.setenv("SMARTSUPPORT_DEPLOYMENT", deployment)
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", enabled)
    service = WebBackend(tmp_path, bind_address=bind)
    assert service.live_allowed() is expected
    visitor = service.new_visitor()
    if not expected:
        with pytest.raises(WebError, match="live_disabled"):
            service.new_conversation(visitor, "عميل تجريبي 1", "Live")
    service.close()


def test_live_configuration_failure_is_redacted_without_constructing_api_client(backend, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_DEPLOYMENT", "local")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    def invalid():
        raise ValueError("private credential or path must not reach UI")
    monkeypatch.setattr("web_runtime.AgentConfig.from_env", invalid)
    monkeypatch.setattr("web_runtime.AsyncOpenAI", lambda **kwargs: pytest.fail("No paid client permitted"))
    visitor = backend.new_visitor()
    identifier = backend.new_conversation(visitor, "عميل تجريبي 1", "Live")
    with pytest.raises(WebError, match="^configuration$"):
        backend.send(visitor, identifier, "مرحبًا")


def test_revoking_live_blocks_requests_but_allows_safe_demo_switch(backend, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_DEPLOYMENT", "local")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    visitor = backend.new_visitor()
    live = backend.new_conversation(visitor, "عميل تجريبي 1", "Live")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "0")
    with pytest.raises(WebError, match="live_disabled"):
        backend.send(visitor, live, "مرحبًا")
    demo = backend.new_conversation(visitor, "عميل تجريبي 1", "Demo", previous=live)
    assert backend.view(visitor, demo)["mode"] == "Demo"
