"""Presentation regression with actual MCP, synthetic isolated stores, no API."""
from decimal import Decimal
import hashlib
import json

import pytest
from streamlit.testing.v1 import AppTest

from config import ROOT
from ui.components import budget_values
from ui.styles import font_css
from web_ui import get_web_backend


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_WEB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("web_runtime.AsyncOpenAI", lambda **kw: pytest.fail("No paid API"))
    get_web_backend.clear()
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    yield app
    get_web_backend.clear()


def text(app):
    return "\n".join(str(e.value) for name in ("text", "caption", "error", "info", "markdown") for e in getattr(app, name))


def conversation(app):
    import os
    import streamlit as st
    backend = get_web_backend(os.environ["SMARTSUPPORT_WEB_DATA_DIR"], st.get_option("server.address") or "")
    return backend.conversations[app.session_state["conversation"]]


@pytest.mark.parametrize("limit,spent,remaining", [("0.02", "0.000008", "0.019992"),
    ("0.01", "0.01", "0"), ("0.005", "0.008", "0")])
def test_budget_uses_actual_limit_and_never_displays_negative_remaining(limit, spent, remaining):
    assert budget_values({"budget_usd": limit, "estimated_cost_usd": spent}) == tuple(map(Decimal, (limit, spent, remaining)))


@pytest.mark.parametrize("result", [{}, {"budget_usd": "0", "estimated_cost_usd": "0"},
    {"budget_usd": "0.02", "estimated_cost_usd": "NaN"}])
def test_missing_or_invalid_budget_is_unavailable_not_zero(result):
    assert budget_values(result) is None


def test_empty_activity_has_no_invented_usage_or_events(workspace):
    assert not workspace.exception
    assert "لم يُستدعَ نموذج بعد" in text(workspace)
    assert len(workspace.metric) == 0
    assert "MCP • تم التحقق" not in text(workspace)
    assert any(e.label == "لوحة النشاط والتكلفة" for e in workspace.expander)


def test_timeline_source_and_budget_come_from_actual_mcp_execution(workspace):
    workspace.button(key="suggest_local_shipping").click().run()
    con = conversation(workspace)
    result = con.result
    events = [json.loads(line) for line in (con.directory / "audit.jsonl").read_text().splitlines()]
    expected = [{key: e[key] for key in ("tool_name", "status", "duration_ms", "error_code", "timestamp")}
                for e in events if e["task_id"] == result["task_id"] and e["event_type"] == "mcp_result"]
    assert result["activity_timeline"] == expected and expected[0]["status"] == "success"
    assert result["budget_usd"] == "0.005"
    assert "مصدر الاستعلام عبر MCP" in text(workspace) and "قاعدة معرفة المتجر" in text(workspace)
    assert "المتبقي تقديريًا" in text(workspace) and "usage_source: simulation" in text(workspace)
    remaining_label = next(c.value for c in workspace.caption if c.value.startswith("المتبقي تقديريًا"))
    assert "USD" in remaining_label and "$" not in remaining_label  # No accidental Markdown math.
    assert not any(key in result["activity_timeline"][0] for key in ("arguments", "session_id", "approval_id", "content"))


def test_pending_is_not_a_tool_event_and_confirmation_keeps_task_budget(workspace):
    workspace.button(key="suggest_support_agent").click().run()
    before = conversation(workspace).result
    assert before["activity_timeline"] == [] and before["tool_call_count"] == 0
    assert "بانتظار موافقة بشرية" in text(workspace)
    next(b for b in workspace.button if b.label == "تأكيد إنشاء الشكوى").click().run()
    after = conversation(workspace).result
    assert after["task_id"] == before["task_id"] and after["budget_usd"] == before["budget_usd"]
    assert Decimal(after["estimated_cost_usd"]) >= Decimal(before["estimated_cost_usd"])
    assert len(after["activity_timeline"]) == 1
    assert after["activity_timeline"][0]["tool_name"] == "create_ticket"
    workspace.run()
    assert len(conversation(workspace).result["activity_timeline"]) == 1


def test_failed_tool_has_no_successful_source_card(workspace):
    workspace.chat_input[0].set_value("ما حالة الطلب DEMO-2001؟").run()
    assert "ACCESS_DENIED" in text(workspace)
    assert "مصدر الاستعلام عبر MCP" not in text(workspace)


def test_hiding_panel_keeps_agent_and_audit_running(workspace):
    workspace.toggle(key="show_activity").set_value(False).run()
    workspace.chat_input[0].set_value("كم تستغرق مدة الشحن؟").run()
    assert not workspace.exception and len(workspace.metric) == 0
    assert "FAQ-001" in text(workspace)
    assert conversation(workspace).result["activity_timeline"][0]["tool_name"] == "search_faq"


def test_bundled_font_integrity_license_and_offline_embedding():
    root = ROOT / "assets" / "fonts"
    expected = {"IBMPlexSansArabic-Regular.ttf": "6F611412270A132BBAC838DA9259D4C68569B4175F3B3B8FA3FA36A30B56DAB9",
                "IBMPlexSansArabic-SemiBold.ttf": "597BD5502E5997BE4414E4C9C88834B30FF3784250C84F20BCE2B20E53EBD467"}
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest().upper() == digest
    license_text = (root / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in license_text and "IBM Corp." in license_text
    assert "data:font/ttf;base64," in font_css() and "https://" not in font_css()
