"""UI regression tests: real MCP + Fake model, isolated synthetic databases."""
import json
import secrets
import sqlite3
import tomllib
from contextlib import closing

import pytest
from streamlit.testing.v1 import AppTest

from config import ROOT
from web_controls import WebError
from web_runtime import WebBackend
from web_ui import get_web_backend

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_WEB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("web_runtime.AsyncOpenAI", lambda **kw: pytest.fail("Paid API forbidden"))
    get_web_backend.clear()
    ui = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    yield ui
    get_web_backend.clear()


def text(ui):
    return "\n".join(str(e.value) for name in ("text", "caption", "error", "info", "markdown", "code") for e in getattr(ui, name))


def backend():
    import os

    import streamlit as st
    return get_web_backend(os.environ["SMARTSUPPORT_WEB_DATA_DIR"], st.get_option("server.address") or "")


def test_no_key_demo_and_mcp_not_claimed_before_initialization(workspace):
    assert not workspace.exception
    assert workspace.selectbox(key="mode").options == ["محاكاة آمنة"]
    assert workspace.selectbox(key="mode").value == "Demo"
    assert "لم يُتحقق بعد" in text(workspace) and "MCP • تم التحقق" not in text(workspace)


@pytest.mark.parametrize("button", ["suggest_assignment_return", "suggest_local_shipping"])
def test_suggestion_executes_real_mcp_and_exact_activity(workspace, button):
    workspace.button(key=button).click().run()
    assert not workspace.exception and "FAQ-" in text(workspace)
    assert "MCP • تم التحقق" in text(workspace)
    service = backend()
    con = service.conversations[workspace.session_state["conversation"]]
    result = con.result
    log = [json.loads(line) for line in (con.directory / "audit.jsonl").read_text().splitlines()]
    events = [e for e in log if e["event_type"] == "mcp_result" and e["task_id"] == result["task_id"]]
    assert result["activity"] == [{k: e[k] for k in ("tool_name", "status", "duration_ms", "error_code")} for e in events]
    assert result["activity"][0]["tool_name"] == "search_faq"
    assert result["tool_call_count"] == sum(e["event_type"] == "mcp_request" for e in log)
    assert result["execution_ms"] >= 0
    assert "usage_source: simulation" in text(workspace) and "بيانات محاكاة" in text(workspace)
    assert "fake-test-model" in text(workspace) and len(workspace.metric) == 3
    assert result["estimated_cost_usd"] in text(workspace)


def test_denied_tool_status_is_reported_as_failure(workspace):
    workspace.chat_input[0].set_value("ما حالة الطلب DEMO-2001؟").run()
    assert "فشل / رفض" in text(workspace) and "ACCESS_DENIED" in text(workspace)
    assert "نجاح · get_order_status" not in text(workspace)


def test_archive_read_only_and_pending_not_replayed(workspace):
    workspace.chat_input[0].set_value("إنشاء شكوى للطلب DEMO-1002").run()
    previous = workspace.session_state["conversation"]
    service = backend()
    directory = service.conversations[previous].directory
    workspace.button(key="new_chat").click().run()
    workspace.selectbox(key="history_selection").select(previous).run()
    assert not workspace.exception and workspace.chat_input[0].disabled
    assert "للقراءة فقط" in text(workspace)
    assert not any(b.label == "تأكيد إنشاء الشكوى" for b in workspace.button)
    with closing(sqlite3.connect(directory / "store.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 3
    workspace.selectbox(key="history_selection").select("current").run()
    assert not workspace.chat_input[0].disabled and workspace.session_state["messages"] == []


def test_archive_ownership_filters_and_snapshot_is_not_mutable(tmp_path):
    service = WebBackend(tmp_path)
    try:
        visitor, other = service.new_visitor(), service.new_visitor()
        old = service.new_conversation(visitor, "عميل تجريبي 1")
        service.send(visitor, old, "ما مدة الشحن؟")
        service.new_conversation(visitor, "عميل تجريبي 1", previous=old)
        assert service.history(other, "عميل تجريبي 1", "Demo") == []
        assert service.history(visitor, "عميل تجريبي 2", "Demo") == []
        copy = service.history(visitor, "عميل تجريبي 1", "Demo")
        copy[0]["messages"][0]["content"] = "mutated"
        assert service.history(visitor, "عميل تجريبي 1", "Demo")[0]["messages"][0]["content"] != "mutated"
        with pytest.raises(WebError):
            service.history("forged", "عميل تجريبي 1", "Demo")
    finally:
        service.close()


def test_pending_blocks_send_and_repeated_approval_does_not_duplicate(workspace):
    workspace.button(key="suggest_support_agent").click().run()
    assert workspace.chat_input[0].disabled and workspace.button(key="save_memory").disabled
    next(b for b in workspace.button if b.label == "تأكيد إنشاء الشكوى").click().run()
    service = backend()
    con = service.conversations[workspace.session_state["conversation"]]
    workspace.run()
    workspace.run()
    with closing(sqlite3.connect(con.directory / "store.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 4
    assert "approval_id" not in text(workspace)
    assert len(con.result["activity"]) == 1
    assert con.result["activity"][0]["tool_name"] == "create_ticket"


def test_copy_uses_native_plain_text_and_no_untrusted_html(workspace, monkeypatch):
    service = backend()
    con = service.conversations[workspace.session_state["conversation"]]
    hostile = "<script>alert('x')</script>" + "نص عربي طويل " * 100
    con.messages.append({"role": "assistant", "content": hostile})
    workspace.toggle(key="copy_answer").set_value(True).run()
    assert not workspace.exception and workspace.code[0].value == hostile
    assert hostile.strip() in [e.value for e in workspace.text]
    assert not any("<script>" in e.value for e in workspace.markdown)


def test_backend_restart_without_recovery_secret_requires_fresh_isolated_context(workspace):
    workspace.button(key="suggest_local_shipping").click().run()
    original_visitor = workspace.session_state["visitor"]
    get_web_backend.clear()
    workspace.run()
    assert not workspace.exception and workspace.session_state["visitor"] == original_visitor
    assert "FAQ-001" in text(workspace)


def test_theme_dual_mode_and_no_external_font_downloads():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert "light" in config["theme"] and "dark" in config["theme"]
    assert "Tahoma" in config["theme"]["font"] and "http" not in str(config["theme"])
    assert not config["server"]["enableStaticServing"]
    assert config["client"]["toolbarMode"] == "viewer"


def test_primary_button_white_text_contrast_in_both_themes():
    theme = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())["theme"]
    for mode in ("light", "dark"):
        color = theme[mode]["primaryColor"].lstrip("#")
        channels = [int(color[i:i+2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in channels]
        luminance = sum(c*w for c, w in zip(linear, (.2126, .7152, .0722)))
        assert 1.05 / (luminance + .05) >= 4.5


def test_live_ui_not_ready_without_key_even_after_operator_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", "0")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    monkeypatch.setenv("SMARTSUPPORT_DEPLOYMENT", "local")
    monkeypatch.setattr("web_runtime.AgentConfig.from_env", lambda: object())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from auth_store import AuthStore
    auth=AuthStore(tmp_path)
    operator_password=secrets.token_urlsafe(24)
    auth.create_user("operator",operator_password,role="operator")
    token,_=auth.login("operator",operator_password)
    auth.set_live_enabled(token,True)
    service = WebBackend(tmp_path)
    try:
        assert service.live_allowed() and not service.live_ready()
    finally:
        service.close()
