import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from streamlit.testing.v1 import AppTest

from config import ROOT
from web_runtime import WebBackend
from web_ui import get_web_backend


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSUPPORT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.delenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", raising=False)
    monkeypatch.setattr("web_runtime.AsyncOpenAI", lambda **kwargs: pytest.fail("No paid clients in UI tests"))
    get_web_backend.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()
    yield at
    get_web_backend.clear()


def backend_for(ui):
    import os
    import streamlit as st
    return get_web_backend(os.environ["SMARTSUPPORT_WEB_DATA_DIR"], st.get_option("server.address") or "")


def tickets(ui):
    backend = backend_for(ui)
    con = backend.conversations[ui.session_state["conversation"]]
    with sqlite3.connect(con.directory / "store.sqlite3") as db:
        return db.execute("SELECT count(*) FROM tickets").fetchone()[0]


def visible(ui):
    return "\n".join(str(e.value) for name in ["text", "caption", "error", "info", "warning"] for e in getattr(ui, name))


def test_app_imports_and_defaults_to_demo_without_credentials(ui):
    assert not ui.exception
    assert ui.title[0].value == "SmartSupport AI"
    assert ui.selectbox(key="mode").options == ["Demo"]
    assert len(ui.selectbox(key="customer").options) == 5
    assert "محاكاة" in visible(ui)


def test_demo_chat_uses_actual_mcp_and_displays_metrics(ui):
    ui.chat_input[0].set_value("كم تستغرق مدة الشحن؟").run()
    assert not ui.exception
    assert "FAQ-001" in visible(ui) and "search_faq" in visible(ui)
    assert "final_answer" in visible(ui) and len(ui.metric) == 3
    backend = backend_for(ui)
    log = backend.conversations[ui.session_state["conversation"]].directory / "audit.jsonl"
    assert any(e["event_type"] == "mcp_result" and e["status"] == "success" for e in map(json.loads, log.read_text().splitlines()))


def test_customer_selection_clears_history_and_prevents_other_orders(ui):
    ui.chat_input[0].set_value("حالة الطلب DEMO-2001؟").run()
    assert "غير متاحة" in visible(ui)
    old = ui.session_state["conversation"]
    ui.selectbox(key="customer").select("عميل تجريبي 2").run()
    assert ui.session_state["conversation"] != old and ui.session_state["messages"] == []
    ui.chat_input[0].set_value("حالة الطلب DEMO-2001؟").run()
    assert "قيد التجهيز" in visible(ui) and not ui.exception


def test_approval_preview_and_confirm_exactly_once_across_reruns(ui):
    ui.chat_input[0].set_value("إنشاء شكوى للطلب DEMO-1002 بسبب عطل الشاشة").run()
    assert tickets(ui) == 3 and ui.chat_input[0].disabled
    approve = next(b for b in ui.button if b.label == "تأكيد إنشاء الشكوى")
    assert "عطل الشاشة" in visible(ui)
    approve.click().run()
    assert tickets(ui) == 4 and "TKT-" in visible(ui)
    ui.run()
    ui.run()
    assert tickets(ui) == 4 and not ui.exception
    assert not any(b.label == "تأكيد إنشاء الشكوى" for b in ui.button)
    assert "approval_id" not in visible(ui)


def test_cancellation_does_not_write(ui):
    ui.chat_input[0].set_value("إنشاء شكوى للطلب DEMO-1002").run()
    next(b for b in ui.button if b.label == "إلغاء العملية").click().run()
    assert tickets(ui) == 3 and "أُلغيت" in visible(ui) and not ui.exception


def test_new_conversation_rotates_context_and_clears_chat(ui):
    ui.chat_input[0].set_value("ما مدة الشحن؟").run()
    old = ui.session_state["conversation"]
    ui.button(key="new_chat").click().run()
    assert ui.session_state["messages"] == [] and ui.session_state["conversation"] != old


def test_memory_preview_requires_consent_and_survives_rerun(ui):
    ui.chat_input[0].set_value("ما مدة الشحن؟").run()
    ui.button(key="save_memory").click().run()
    assert "الملخص المراد حفظه" in visible(ui)
    next(b for b in ui.button if b.label == "تأكيد حفظ الذاكرة").click().run()
    ui.run()
    ui.button(key="load_memory").click().run()
    assert "الملخص المحفوظ" in visible(ui) and "load_memory" in visible(ui)
    assert not ui.exception


def test_ui_errors_do_not_expose_api_keys_or_exception_details(ui, monkeypatch):
    secret = "sk-synthetic-private-key-for-redaction"
    def failure(*args, **kwargs):
        raise RuntimeError(secret + " private database path")
    monkeypatch.setattr(WebBackend, "send", failure)
    ui.chat_input[0].set_value("ما مدة الشحن؟").run()
    assert not ui.exception and len(ui.error) == 1
    assert secret not in visible(ui) and "private database path" not in visible(ui)


def test_current_capstone_logs_not_modified_by_ui(ui):
    files = [ROOT / "traces" / name for name in ["success_1.jsonl", "near_failure_recovery.jsonl", "explicit_failure.jsonl"]]
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in files]
    ui.chat_input[0].set_value("ما مدة الضمان؟").run()
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in files] == before


def test_paid_smoke_command_requires_explicit_flag():
    result = subprocess.run([sys.executable, str(ROOT / "live_smoke.py")], capture_output=True, timeout=15)
    assert result.returncode != 0 and b"--allow-paid" in result.stderr
