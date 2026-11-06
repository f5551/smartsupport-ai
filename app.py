"""Arabic workspace. All execution and authority remain in WebBackend."""
import streamlit as st
import secrets
from runtime_policy import direct_demo_allowed, public_demo_only, web_data_directory
from ui.components import approval, chat, header, insights, suggestions
from ui.styles import apply_styles
from ui.pages import PAGES, NAV_LABELS, navigate, dashboard, orders, tickets, tools_and_security, settings
from web_controls import WebError
from web_runtime import CUSTOMERS, ERRORS
from web_ui import get_web_backend
from auth_store import Principal

st.set_page_config(page_title="SmartSupport AI | دعم المتجر", page_icon=":material/support_agent:", layout="wide")
apply_styles()
for key, default in {"demo_user_id": None, "visitor": None, "conversation": None, "messages": [], "selection": None, "notice": None}.items():
    if key not in st.session_state:
        st.session_state[key] = default

try:
    bind_address = st.get_option("server.address") or ""
    backend = get_web_backend(str(web_data_directory()), bind_address)
except Exception:
    st.error("تعذر إعداد الخدمة. راجع المشغل؛ لم تُعرض تفاصيل حساسة.")
    st.stop()

if not direct_demo_allowed(bind_address):
    st.error("الدخول المباشر متاح فقط في وضع Demo المحلي أو العرض التجريبي العام، مع تعطيل Live.")
    st.stop()
if st.session_state.demo_user_id is None:
    st.session_state.demo_user_id = secrets.randbits(63)
principal = Principal(st.session_state.demo_user_id, "local-demo", "customer", "demo_customer_001")
customer=next(label for label, customer_id in CUSTOMERS.items() if customer_id==principal.customer_id)
try:
    if not st.session_state.visitor or st.session_state.visitor not in backend.visitors:
        st.session_state.visitor=backend.restore_account(principal)
    if st.session_state.visitor not in backend.visitors:
        st.session_state.visitor=backend.create_account_session(principal)
    owner=backend.visitors[st.session_state.visitor]
    if owner.user_id != principal.user_id or owner.customer_id != principal.customer_id:
        raise WebError("session_lost")
    if not st.session_state.conversation:
        candidate=backend.journal.latest_active_conversation(st.session_state.visitor)
        if candidate:
            con=backend._get(st.session_state.visitor,candidate,enforce_live=False)
            if con.mode=="Demo" or backend.live_allowed():
                st.session_state.conversation=candidate
                st.session_state.selection=(customer,con.mode)
                st.session_state.mode=con.mode
except Exception:
    st.error("تعذر استعادة مساحة التجربة بأمان. افتح جلسة متصفح جديدة.")
    st.stop()

with st.sidebar:
    with st.container(key="sidebar_brand"):
        st.markdown(":material/support_agent: **SmartSupport AI**", text_alignment="right")
        st.caption("دعم أذكى لعملاء أسعد.", text_alignment="right")
    with st.container(key="page_nav"):
        page = st.radio("التنقل", PAGES, index=1, format_func=NAV_LABELS.get, key="page", label_visibility="collapsed", width="stretch")
    reset = st.button("بدء محادثة جديدة", icon=":material/add_comment:", type="primary", width="stretch", key="new_chat",
                      on_click=navigate,args=("المحادثات",))
    history_slot = st.expander("سجل المحادثات", expanded=page=="المحادثات", icon=":material/history:")
    st.divider()
    st.caption("إعدادات التجربة", text_alignment="right")
    mode = st.selectbox("وضع التشغيل", ["Demo", "Live"] if backend.live_ready() else ["Demo"], key="mode",
                        format_func=lambda value: "محاكاة آمنة" if value=="Demo" else "تشغيل مباشر محلي")
    st.caption(f"جلسة تجريبية مباشرة · {customer}", text_alignment="right")
    with st.expander("إعدادات العرض", icon=":material/tune:"):
        show_activity = st.toggle("عرض نشاط الوكيل والتكلفة", value=True, key="show_activity")
        copy_answer = st.toggle("إتاحة نسخ آخر إجابة", value=False, key="copy_answer")
        st.caption("للمظهر الداكن: افتح قائمة ⋮ أعلى الصفحة، ثم اختر المظهر الداكن.", text_alignment="right")
    with st.expander("عن مساحة التجربة", icon=":material/info:"):
        st.caption("واجهة عربية · محادثات وطلبات وتذاكر دعم", text_alignment="right")
        st.caption("حد الرسالة 1000 محرف. 10 طلبات/دقيقة و30 لكل جلسة، وحد عام للخدمة.", text_alignment="right")
        st.caption("البيانات اصطناعية. لا تشارك كلمات مرور أو بيانات دفع.", text_alignment="right")
        if not backend.live_allowed():
            st.caption("التشغيل المباشر معطل للزوار. يتطلب تشغيلًا محليًا مقيدًا وإعداد المشغل.", text_alignment="right")
    if public_demo_only():
        st.caption("عرض عام مؤقت: قد تُحذف المحادثات عند نوم الخدمة أو إعادة تشغيلها.", text_alignment="right")
    with st.container(key="session_summary"):
        st.caption(":material/lock: الجلسة معزولة في هذا المتصفح", text_alignment="right")
        st.caption("محاكاة آمنة · بيانات اصطناعية" if mode == "Demo" else "تشغيل مباشر · محلي", text_alignment="right")
try:
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
        st.session_state.conversation = None
        st.session_state.messages = []
        st.rerun()
    st.stop()
except Exception:
    st.error("تعذر استعادة المحادثة بأمان. ابدأ جلسة متصفح جديدة.")
    st.stop()
with history_slot:
    names = {"current": "المحادثة الحالية"}
    for index, archive in enumerate(archives, 1):
        names[archive["id"]] = f"محادثة سابقة {index} · للقراءة فقط"
    query = st.text_input("بحث في محادثاتك", key="history_search", placeholder="ابحث في نص المحادثة…", max_chars=100)
    if query.strip():
        matches = {a["id"] for a in archives if query.strip() in " ".join(m["content"] for m in a["messages"])}
        names = {identifier: label for identifier, label in names.items() if identifier == "current" or identifier in matches}
    chosen = st.selectbox("محادثات هذه الجلسة", list(names), format_func=names.get, key="history_selection")
    st.caption("تظهر محادثات هذا العميل في جلسة المتصفح الحالية فقط.", text_alignment="right")
archived = chosen != "current" and page == "المحادثات"
if archived:
    view = next(a for a in archives if a["id"] == chosen)
st.session_state.messages = view["messages"]
header(mode, customer, view["mcp_status"])
if archived:
    st.info("عرض سابق للقراءة فقط. ارجع إلى المحادثة الحالية للإرسال. الموافقات القديمة لا تُستأنف من الأرشيف.")
result = view["result"]
pending = (result or {}).get("pending_approval")
prompt = None
if page == "الرئيسية":
    dashboard(view, archives)
elif page == "الأدوات والأمان":
    tools_and_security(view)
elif page == "الإعدادات":
    settings(customer, mode)
elif page in {"الطلبات", "التذاكر"}:
    prompt = orders(result, bool(pending)) if page == "الطلبات" else tickets(result, bool(pending))
    if st.session_state.notice:
        st.error(st.session_state.notice)
    if pending:
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
    if result and show_activity:
        with st.expander("لوحة النشاط والتكلفة", icon=":material/monitoring:"):
            insights(result, view.get("model"))
if pending and page in {"الرئيسية", "الأدوات والأمان", "الإعدادات"}:
    st.warning("لديك عملية بانتظار المراجعة. لن تُنفذ قبل موافقتك.")
    st.button("مراجعة العملية المعلقة", on_click=navigate, args=("المحادثات",), key="review_pending")
if page == "المحادثات":
    st.header("المحادثات", anchor=False)
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
        with st.spinner("جارٍ تجهيز الإجابة من بيانات المتجر…"):
            backend.send(st.session_state.visitor, st.session_state.conversation, prompt)
        st.session_state.notice = None
    except WebError as error:
        st.session_state.notice = ERRORS.get(error.code, ERRORS["mcp_error"])
    except Exception:
        st.session_state.notice = ERRORS["mcp_error"]
    st.rerun()
with st.container(key="footer"):
    st.caption("SmartSupport AI · دعم يستند إلى بيانات المتجر · الموافقة قبل كل كتابة", text_alignment="right")
