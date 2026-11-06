"""Reference UI flows: real MCP + Fake OpenAI and isolated synthetic accounts."""
import sqlite3

import pytest
from test_ui_redesign import backend, text
from test_ui_redesign import workspace as _workspace

from ui.pages import PAGES, TOOLS, order_status

workspace = _workspace  # Expose the imported pytest fixture to this module.


def go(app, page):
    app.radio(key="page").set_value(page).run()
    assert not app.exception


def ticket_count(app):
    con = backend().conversations[app.session_state["conversation"]]
    db = sqlite3.connect(con.directory / "store.sqlite3")
    try:
        return db.execute("SELECT count(*) FROM tickets").fetchone()[0]
    finally:
        db.close()


def test_navigation_only_renders_and_preserves_authenticated_conversation(workspace, monkeypatch):
    # Preserve the historical test name; the contract is now an isolated Demo session.
    identifier = workspace.session_state["conversation"]
    visitor = workspace.session_state["visitor"]
    demo_identity = workspace.session_state["demo_user_id"]
    monkeypatch.setattr(backend(), "send", lambda *a, **kw: pytest.fail("Navigation must never send a task"))
    for page in PAGES:
        go(workspace, page)
        assert workspace.session_state["conversation"] == identifier
        assert workspace.session_state["visitor"] == visitor
        assert workspace.session_state["demo_user_id"] == demo_identity
        assert not any(field.key in {"login_username", "login_password"} for field in workspace.text_input)
    go(workspace, "المحادثات")
    assert workspace.chat_input and not workspace.chat_input[0].disabled


def test_navigation_keeps_another_demo_browser_session_isolated(workspace, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from config import ROOT
    from web_controls import WebError

    marker = "رسالة اصطناعية خاصة بالجلسة الأولى"
    workspace.chat_input[0].set_value(marker).run()
    assert not workspace.exception
    first = tuple(workspace.session_state[key] for key in ("demo_user_id", "visitor", "conversation"))
    other = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    assert not other.exception
    second = tuple(other.session_state[key] for key in ("demo_user_id", "visitor", "conversation"))
    assert all(a != b for a, b in zip(first, second))
    service = backend()
    monkeypatch.setattr(service, "send", lambda *a, **kw: pytest.fail("Navigation must never send a task"))
    for page in PAGES:
        go(other, page)
        assert tuple(other.session_state[key] for key in ("demo_user_id", "visitor", "conversation")) == second
        assert marker not in text(other)
        with pytest.raises(WebError):
            service.view(second[1], first[2])
    go(workspace, "المحادثات")
    assert marker in text(workspace)
    assert tuple(workspace.session_state[key] for key in ("demo_user_id", "visitor", "conversation")) == first


def test_dashboard_empty_state_never_invents_statistics(workspace):
    go(workspace, "الرئيسية")
    assert [m.value for m in workspace.metric] == ["1", "0", "0", "—"]
    assert "لا توجد عمليات أدوات مسجلة" in text(workspace)
    assert "86%" not in text(workspace) and "$2.40" not in text(workspace)


def test_order_search_runs_real_mcp_and_never_invents_delivery_date(workspace):
    go(workspace, "الطلبات")
    workspace.text_input(key="order_lookup_id").set_value("DEMO-1001")
    next(b for b in workspace.button if b.label == "بحث عن الطلب").click().run()
    assert not workspace.exception
    con = backend().conversations[workspace.session_state["conversation"]]
    assert con.result["tools_used"] == ["get_order_status"]
    assert order_status(con.result) == ("DEMO-1001", "قيد الشحن")
    assert "لا يتوفر موعد توصيل متوقع" in text(workspace)
    assert "8 نوفمبر" not in text(workspace)
    workspace.text_input(key="order_lookup_id").set_value("DEMO-2001")
    next(b for b in workspace.button if b.label == "بحث عن الطلب").click().run()
    assert not workspace.exception and order_status(con.result) is None
    assert "غير متاحة" in text(workspace)


def test_ticket_page_requires_consent_and_creates_only_one_ticket(workspace):
    go(workspace, "التذاكر")
    before = ticket_count(workspace)
    # Fields inside the two separate native forms, located by their Arabic labels.
    next(t for t in workspace.text_input if t.label == "رقم الطلب المرتبط بالشكوى").set_value("DEMO-1002")
    workspace.text_area[0].set_value("عطل اصطناعي في الشاشة")
    next(b for b in workspace.button if b.label == "مراجعة طلب الدعم").click().run()
    assert not workspace.exception and ticket_count(workspace) == before
    assert "عطل اصطناعي" in text(workspace)
    assert not any(b.label == "مراجعة طلب الدعم" for b in workspace.button)
    go(workspace, "الأدوات والأمان")
    assert any("لديك عملية بانتظار المراجعة" in warning.value for warning in workspace.warning)
    go(workspace, "التذاكر")
    next(b for b in workspace.button if b.label == "تأكيد إنشاء الشكوى").click().run()
    assert not workspace.exception and ticket_count(workspace) == before + 1
    assert "TKT-" in text(workspace)
    workspace.run()
    assert ticket_count(workspace) == before + 1
    assert "approval_id" not in text(workspace) and "write_receipts" not in text(workspace)


def test_tools_show_exact_six_capabilities_and_no_invented_controls(workspace):
    go(workspace, "الأدوات والأمان")
    identifiers = {c.value for c in workspace.code}
    assert identifiers == {row[0] for row in TOOLS}
    assert len(identifiers) == 6
    assert "تصعيد لموظف" not in text(workspace)
    assert "لم يُستدعَ نموذج بعد" in text(workspace)


def test_order_card_rejects_unverified_or_model_guessed_status():
    assert order_status({"stop_reason": "final_answer", "message": "الطلب DEMO-1001؛ الحالة: تم التسليم.", "activity": []}) is None
    assert order_status({"stop_reason": "mcp_error", "message": "قيد الشحن"}) is None
