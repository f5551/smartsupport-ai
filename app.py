"""Arabic workspace. All execution and authority remain in WebBackend."""
import streamlit as st
from runtime_policy import public_demo_only, web_data_directory
from ui.components import approval, chat, header, insights, suggestions
from ui.styles import apply_styles
from web_controls import WebError
from web_runtime import CUSTOMERS, ERRORS
from web_ui import get_web_backend

st.set_page_config(page_title="SmartSupport AI | دعم المتجر", page_icon=":material/support_agent:", layout="wide")
apply_styles()
for key, default in {"visitor": None, "recovery_secret": None, "conversation": None, "messages": [], "selection": None, "notice": None}.items():
    if key not in st.session_state:
        st.session_state[key] = default
try:
    backend = get_web_backend(str(web_data_directory()), st.get_option("server.address") or "")
except Exception:
    st.error("تعذر إعداد الخدمة. راجع المشغل؛ لم تُعرض تفاصيل حساسة.")
    st.stop()

with st.sidebar:
    with st.container(key="sidebar_brand"):
        st.markdown(":material/support_agent: **SmartSupport AI**", text_alignment="right")
        st.caption("مساحة دعم واحدة، تجربة أوضح.", text_alignment="right")
    with st.expander("استعادة جلسة محفوظة", icon=":material/lock:"):
        st.caption("يلزم المعرف وسر الاستعادة الصادران لهذه الجلسة. المعرف وحده لا يكفي؛ هذه ليست مصادقة حساب عميل.")
        with st.form('restore_session'):
            restore_id=st.text_input('معرف الجلسة المحفوظة',key='restore_id')
            restore_secret=st.text_input('سر الاستعادة',type='password',key='restore_secret')
            restore_submit=st.form_submit_button('استعادة دون تنفيذ مهام')
        if restore_submit:
            try:
                restored=backend.restore_visitor(restore_id,restore_secret)
                available=[row for row in restored if row['active'] and (row['mode']=='Demo' or backend.live_allowed())]
                if not available:
                    raise WebError('session_lost')
                selected=available[-1]
                st.session_state.visitor=restore_id
                st.session_state.recovery_secret=restore_secret
                st.session_state.conversation=selected['id']
                st.session_state.selection=(selected['customer_label'],selected['mode'])
                st.session_state.customer=selected['customer_label']
                st.session_state.mode=selected['mode']
                st.session_state.history_selection='current'
                st.session_state.notice=None
                st.rerun()
            except WebError:
                st.error("تعذرت الاستعادة المصرح بها أو انتهت الجلسة. لا تكفي معرفة المعرف؛ ابدأ جلسة جديدة إن فقدت السر.")
            except Exception:
                st.error("تعذرت الاستعادة بأمان؛ لم تُنفذ أي مهمة.")
    reset = st.button("بدء محادثة جديدة", icon=":material/add_comment:", type="primary", width="stretch", key="new_chat")
    history_slot = st.container()
    st.divider()
    st.caption("إعدادات التجربة", text_alignment="right")
    mode = st.selectbox("وضع التشغيل", ["Demo", "Live"] if backend.live_ready() else ["Demo"], key="mode")
    customer = st.selectbox("العميل التجريبي", list(CUSTOMERS), key="customer")
    st.caption("اختيار العميل للعرض فقط، وليس تسجيل دخول أو مصادقة حقيقية.", text_alignment="right")
    with st.expander("إعدادات العرض", icon=":material/tune:"):
        show_activity = st.toggle("عرض نشاط الوكيل والتكلفة", value=True, key="show_activity")
        copy_answer = st.toggle("إتاحة نسخ آخر إجابة", value=False, key="copy_answer")
        st.caption("للمظهر الداكن: افتح قائمة ⋮ أعلى الصفحة، ثم اختر Dark ضمن Theme.", text_alignment="right")
    with st.expander("عن مساحة التجربة", icon=":material/info:"):
        st.caption("واجهة 3.0 · Streamlit 1.66.1 · MCP 1.27.2", text_alignment="right")
        st.caption("حد الرسالة 1000 محرف. 10 طلبات/دقيقة و30 لكل جلسة، وحد عام للخدمة.", text_alignment="right")
        st.caption("البيانات اصطناعية. لا تشارك كلمات مرور أو بيانات دفع.", text_alignment="right")
        if not backend.live_allowed():
            st.caption("Live معطل للزوار. يتطلب تشغيلًا محليًا مقيدًا وإعداد المشغل.", text_alignment="right")
    if public_demo_only():
        st.caption("عرض عام مؤقت: قد تُحذف المحادثات عند نوم الخدمة أو إعادة تشغيلها.", text_alignment="right")
    with st.container(key="session_summary"):
        st.caption(":material/lock: الجلسة خاصة بهذا المتصفح", text_alignment="right")
        st.caption("Demo · بيانات اصطناعية" if mode == "Demo" else "Live · تشغيل محلي", text_alignment="right")
try:
    if not st.session_state.visitor:
        st.session_state.visitor,st.session_state.recovery_secret = backend.create_browser_session()
    elif st.session_state.visitor not in backend.visitors:
        backend.restore_visitor(st.session_state.visitor,st.session_state.recovery_secret)
    selection = (customer, mode)
    if reset or not st.session_state.conversation or st.session_state.selection != selection:
        st.session_state.conversation = backend.new_conversation(st.session_state.visitor, customer, mode,
                                                                previous=st.session_state.conversation)
        st.session_state.selection = selection
        st.session_state.messages = []
        st.session_state.notice = None
        st.session_state.history_selection = "current"
    view = backend.view(st.session_state.visitor, st.session_state.conversation)
    archives = backend.history(st.session_state.visitor, customer, mode)
except WebError as error:
    st.error(ERRORS.get(error.code, ERRORS["session_lost"]))
    if error.code == "session_lost" and st.button("إعادة تهيئة محادثة آمنة", key="recover"):
        st.session_state.visitor = None
        st.session_state.recovery_secret = None
        st.session_state.conversation = None
        st.session_state.messages = []
        st.rerun()
    st.stop()
except Exception:
    st.error("تعذر استعادة المحادثة بأمان. ابدأ جلسة متصفح جديدة.")
    st.stop()
with st.sidebar, st.expander('بيانات استعادة هذه الجلسة',icon=':material/key:'):
    st.caption('تُحفظ محادثة العرض لاستعادتها؛ حفظ ملخص الذاكرة إجراء منفصل بموافقتك.')
    st.caption('احفظهما في مكان خاص قبل إغلاق المتصفح. لا تشارك السر ولا تضعه في رسالة الدعم أو رابط. لا استعادة بعد انتهاء الخمول، حتى بالسر الصحيح.')
    st.text_input('معرف هذه الجلسة',value=st.session_state.visitor or '',disabled=True,key='export_session_id_'+st.session_state.visitor)
    st.text_input('سر هذه الجلسة',value=st.session_state.recovery_secret or '',type='password',disabled=True,key='export_recovery_secret_'+st.session_state.visitor)
with history_slot:
    names = {"current": "المحادثة الحالية"}
    for index, archive in enumerate(archives, 1):
        names[archive["id"]] = f"محادثة سابقة {index} · للقراءة فقط"
    chosen = st.selectbox("محادثات هذه الجلسة", list(names), format_func=names.get, key="history_selection")
    st.caption("تظهر محادثات هذا العميل في جلسة المتصفح الحالية فقط.", text_alignment="right")
archived = chosen != "current"
if archived:
    view = next(a for a in archives if a["id"] == chosen)
st.session_state.messages = view["messages"]
header(mode, customer, view["mcp_status"])
if archived:
    st.info("عرض سابق للقراءة فقط. ارجع إلى المحادثة الحالية للإرسال. الموافقات القديمة لا تُستأنف من الأرشيف.")
result = view["result"]
pending = (result or {}).get("pending_approval")
with st.container(key="workspace_grid"):
    if show_activity:
        chat_column, activity_column = st.columns([2.15, 1], gap="medium")
    else:
        chat_column, activity_column = st.container(), None
    with chat_column:
        with st.container(key="main_workspace"):
            prompt = None
            if not view["messages"] and not archived:
                prompt = suggestions(list(CUSTOMERS).index(customer) + 1)
            chat(view["messages"], copy_answer=copy_answer, result=result)
            if st.session_state.notice:
                st.error(st.session_state.notice)
            if result and result["stop_reason"] not in {"final_answer", "pending_approval"}:
                st.warning("توقفت المهمة دون تأكيد اكتمالها. راجع السبب ولا تكرر كتابة غير مؤكدة.")
            if pending and not archived:
                decision = approval(pending)
                if decision:
                    try:
                        with st.spinner("جارٍ معالجة قرارك…"):
                            backend.decide(st.session_state.visitor, st.session_state.conversation, pending["pending_id"], decision)
                        st.session_state.notice = None
                    except WebError as error:
                        st.session_state.notice = ERRORS.get(error.code, ERRORS["unknown_result"])
                    except Exception:
                        st.session_state.notice = ERRORS["unknown_result"]
                    st.rerun()
            if not archived:
                with st.container(horizontal=True, horizontal_alignment="left"):
                    save = st.button("حفظ ملخص المحادثة", key="save_memory", disabled=bool(pending) or not bool(view["messages"]), icon=":material/bookmark_add:", wrap=True)
                    load = st.button("استرجاع الذاكرة", key="load_memory", disabled=bool(pending), icon=":material/history:")
                if save:
                    summary = "\n".join(m["content"] for m in view["messages"][-4:])[:600]
                    prompt = "احفظ هذا الملخص بعد موافقتي: " + summary
                if load:
                    prompt = "استرجع الملخص المحفوظ لهذه المحادثة"
            typed = st.chat_input("اكتب سؤالك أو رقم الطلب التجريبي…", key="chat", max_chars=1000,
                                  disabled=bool(pending) or archived, submit_mode="disable")
            if typed:
                prompt = typed
    if activity_column is not None:
        with activity_column:
            with st.container(key="agent_workspace"):
                with st.expander("لوحة النشاط والتكلفة", expanded=True, icon=":material/monitoring:"):
                    with st.container(height=660, border=False, key="activity_scroll"):
                        insights(result, view.get("model"))
if prompt and not pending and not archived:
    try:
        with chat_column, st.spinner("جارٍ تجهيز الإجابة من بيانات المتجر…"):
            backend.send(st.session_state.visitor, st.session_state.conversation, prompt)
        st.session_state.notice = None
    except WebError as error:
        st.session_state.notice = ERRORS.get(error.code, ERRORS["mcp_error"])
    except Exception:
        st.session_state.notice = ERRORS["mcp_error"]
    st.rerun()
st.caption("SmartSupport AI · دعم يستند إلى بيانات المتجر · الموافقة قبل كل كتابة", text_alignment="right")
