"""Presentation only: render session-scoped snapshots and return user prompts.

No database queries, tool execution, approval issuance, or model clients here.
"""
from collections import Counter
from datetime import datetime
import re
import streamlit as st

from ui.components import insights
from web_runtime import redact

PAGES = ["الرئيسية", "المحادثات", "الطلبات", "التذاكر", "الأدوات والأمان", "الإعدادات"]
NAV_LABELS = dict(zip(PAGES, [":material/home: الرئيسية", ":material/chat_bubble: المحادثات",
    ":material/package_2: الطلبات", ":material/confirmation_number: التذاكر",
    ":material/security: الأدوات والأمان", ":material/settings: الإعدادات"]))
TOOLS = [
    ("search_faq", "الإجابة من قاعدة المعرفة", "سياسات الشحن والاسترجاع والضمان.", False, "forum"),
    ("get_order_status", "متابعة الطلبات", "قراءة حالة طلب للعميل التجريبي المحدد لهذه الجلسة.", False, "local_shipping"),
    ("get_ticket_status", "متابعة التذاكر", "قراءة حالة تذكرة للعميل التجريبي المحدد لهذه الجلسة.", False, "description"),
    ("create_ticket", "إنشاء تذكرة دعم", "تُعرض التفاصيل للمراجعة قبل تنفيذ العملية.", True, "confirmation_number"),
    ("load_memory", "استرجاع ذاكرة المحادثة", "قراءة الملخص الخاص بهذه الجلسة.", False, "history"),
    ("save_memory", "حفظ ملخص المحادثة", "حفظ المحتوى المعروض بعد موافقة صريحة.", True, "bookmark_add"),
]


def navigate(page):
    st.session_state.page = page


def overview_values(views):
    results = [v["result"] for v in views if v.get("result")]
    return len(views), sum(r.get("stop_reason") == "final_answer" for r in results), sum(bool(r.get("pending_approval")) for r in results)


def dashboard(view, archives):
    views = [view, *archives]
    count, completed, waiting = overview_values(views)
    with st.container(key="dashboard_hero"):
        st.header("مرحبًا بك في SmartSupport AI", anchor=False, text_alignment="right")
        st.text("مساعدك الذكي لخدمة العملاء، من أول سؤال إلى متابعة طلبك.", text_alignment="right")
    with st.container(key="overview_cards"):
        for col, label, value in zip(st.columns(4),
                ["محادثات هذه المساحة", "آخر إجابات مكتملة", "بانتظار الموافقة", "تكلفة المهمة الحالية"],
                [str(count), str(completed), str(waiting), (view.get("result") or {}).get("estimated_cost_usd", "—")]):
            col.metric(label, value)
    st.caption("المؤشرات تخص محادثات الجلسة التجريبية المتاحة هنا؛ التكلفة بالدولار، وتكون محاكاة في وضع التجربة.", text_alignment="right")
    activity = [event for v in views for event in (v.get("result") or {}).get("activity_timeline", [])]
    left, right = st.columns([1.5, 1], gap="medium")
    with left, st.container(border=True):
        st.subheader("نشاط المحادثات", icon=":material/bar_chart:", anchor=False, text_alignment="right")
        st.caption("استدعاءات الأدوات المسجلة في آخر مهمة لكل محادثة · التوقيت العالمي", text_alignment="right")
        counts = Counter()
        for event in activity:
            try:
                counts[datetime.fromisoformat(event["timestamp"]).hour] += 1
            except (KeyError, ValueError, TypeError):
                continue
        if counts:
            st.bar_chart({"الساعة": [f"{h:02d}:00" for h in range(24)],
                          "استدعاءات الأدوات": [counts[h] for h in range(24)]},
                         x="الساعة", y="استدعاءات الأدوات", color="#12B6A6", height=245,
                         alt="عدد استدعاءات أدوات الجلسة التجريبية حسب ساعة التنفيذ")
        else:
            st.info("ابدأ محادثتك الأولى ليظهر نشاطك هنا.", icon=":material/insert_chart:")
            st.button("ابدأ محادثة", on_click=navigate, args=("المحادثات",), key="dashboard_chat", type="primary")
    with right, st.container(border=True):
        st.subheader("آخر النشاطات", icon=":material/schedule:", anchor=False, text_alignment="right")
        if not activity:
            st.caption("لا توجد عمليات أدوات مسجلة بعد.", text_alignment="right")
        names = {name: label for name, label, *_ in TOOLS}
        for event in sorted(activity, key=lambda e: e.get("timestamp", ""), reverse=True)[:5]:
            st.text(names.get(event.get("tool_name"), "عملية أداة"), text_alignment="right")
            st.badge("تم التنفيذ" if event.get("status") == "success" else "تعذر التنفيذ",
                     color="green" if event.get("status") == "success" else "red")
        st.button("عرض المحادثات", on_click=navigate, args=("المحادثات",), key="dashboard_all")


def order_status(result):
    """Parse only the already-authoritative public answer, never infer an ETA."""
    if not result or result.get("stop_reason") != "final_answer":
        return None
    if not any(e.get("tool_name") == "get_order_status" and e.get("status") == "success" for e in result.get("activity", [])):
        return None
    match = re.fullmatch(r"الطلب (DEMO-\d{4})؛ الحالة: (قيد التجهيز|قيد الشحن|تم التسليم|ملغي)\.", result.get("message", ""))
    return match.groups() if match else None


def orders(result, blocked):
    st.header("متابعة الطلبات", anchor=False, text_alignment="right")
    st.caption("ابحث برقم الطلب للحصول على حالته من بيانات العميل التجريبي المحدد لهذه الجلسة.", text_alignment="right")
    with st.container(border=True, key="search_panel"), st.form("order_lookup"):
        field, action = st.columns([3, 1], vertical_alignment="bottom")
        identifier = field.text_input("رقم الطلب", placeholder="مثال: DEMO-1001", key="order_lookup_id", max_chars=128, disabled=blocked)
        submit = action.form_submit_button("بحث عن الطلب", icon=":material/search:", type="primary", width="stretch", disabled=blocked)
    status = order_status(result)
    if status:
        identifier_found, label = status
        with st.container(border=True, key="order_card"):
            st.subheader("الطلب " + identifier_found, icon=":material/package_2:", anchor=False, text_alignment="right")
            st.badge(label, color="red" if label == "ملغي" else "green", icon=":material/local_shipping:")
            st.caption("الحالة الحالية من أداة متابعة الطلبات عبر MCP.", text_alignment="right")
            if label != "ملغي":
                with st.container(key="order_steps"):
                    stages = ["قيد التجهيز", "قيد الشحن", "تم التسليم"]
                    for col, stage in zip(st.columns(3), stages):
                        col.markdown((":material/check_circle: " if stages.index(stage) <= stages.index(label) else ":material/radio_button_unchecked: ") + stage, text_alignment="right")
            st.caption("لا يتوفر موعد توصيل متوقع في النتيجة الحالية.", text_alignment="right")
    elif result and "get_order_status" in result.get("tools_used", []):
        st.info(redact(result.get("message", "لم تتوفر نتيجة قابلة للتحقق.")))
    else:
        st.info("أدخل رقم طلبك لعرض نتيجة فعلية؛ لن تظهر بيانات طلب قبل البحث.", icon=":material/search:")
    if submit:
        if not re.fullmatch(r"DEMO-\d{4}", identifier.strip().upper()):
            st.error("أدخل رقم طلب تجريبي صحيحًا، مثل DEMO-1001.")
        else:
            return "ما حالة الطلب " + identifier.strip().upper() + "؟"


def tickets(result, blocked):
    st.header("تذاكر الدعم", anchor=False, text_alignment="right")
    st.caption("جهّز طلب دعم أو تابع تذكرة سابقة. لن تُنشأ تذكرة قبل مراجعتك وموافقتك.", text_alignment="right")
    if blocked:
        return  # Focus the page on the exact approval preview rendered by app.py.
    with st.container(border=True), st.form("ticket_lookup"):
        identifier = st.text_input("رقم التذكرة", placeholder="رقم التذكرة الذي ظهر عند إنشائها", max_chars=128, disabled=blocked)
        lookup = st.form_submit_button("متابعة التذكرة", icon=":material/search:", disabled=blocked)
    if lookup:
        if re.fullmatch(r"(?:DEMO-TKT-\d{3}|TKT-[a-f0-9]{32})", identifier.strip(), re.I):
            return "ما حالة التذكرة " + identifier.strip() + "؟"
        st.error("تحقق من رقم التذكرة ثم أعد البحث.")
    with st.container(border=True), st.form("ticket_draft"):
        st.subheader("طلب دعم جديد", icon=":material/confirmation_number:", anchor=False, text_alignment="right")
        order = st.text_input("رقم الطلب المرتبط بالشكوى", placeholder="مثال: DEMO-1002", max_chars=128, disabled=blocked)
        description = st.text_area("صف المشكلة", placeholder="اكتب تفاصيل المشكلة التي تريد إرسالها إلى الدعم…", max_chars=700, disabled=blocked)
        draft = st.form_submit_button("مراجعة طلب الدعم", type="primary", icon=":material/preview:", disabled=blocked)
    if draft:
        if not re.fullmatch(r"DEMO-\d{4}", order.strip().upper()) or not description.strip():
            st.error("أدخل رقم الطلب التجريبي ووصف المشكلة قبل المراجعة.")
        else:
            return "أريد إنشاء شكوى للطلب " + order.strip().upper() + " بسبب: " + description.strip()
    if result and not blocked and any(t in result.get("tools_used", []) for t in ("create_ticket", "get_ticket_status")):
        st.info(redact(result.get("message", "لم تتوفر نتيجة قابلة للتحقق.")), icon=":material/confirmation_number:")


def tools_and_security(view):
    st.header("الأدوات والأمان", anchor=False, text_alignment="right")
    st.caption("أدوات المساعد وصلاحيات التنفيذ · حالة الأدوات للعرض، وإعداداتها يفرضها الخادم.", text_alignment="right")
    for name, label, description, write, icon in TOOLS:
        with st.container(border=True):
            body, state = st.columns([4, 1], vertical_alignment="center")
            body.markdown(f":material/{icon}: **{label}**", text_alignment="right")
            body.caption(description, text_alignment="right")
            state.badge("يتطلب موافقة" if write else "قراءة فقط", color="violet" if write else "green")
            with st.expander("معرّف الأداة", expanded=False):
                st.code(name, language=None)
    with st.container(border=True, key="security_panel"):
        st.subheader("إعدادات الأمان", icon=":material/verified_user:", anchor=False, text_alignment="right")
        for col, label in zip(st.columns(3), ["ذاكرة معزولة بالعميل التجريبي والجلسة", "موافقة قبل كل كتابة", "جلسة تجريبية دون تسجيل دخول"]):
            col.markdown(":material/check_circle: " + label, text_alignment="right")
    st.subheader("الاستهلاك والتكلفة", anchor=False, text_alignment="right")
    insights(view.get("result"), view.get("model"))


def settings(customer, mode):
    st.header("إعدادات المساحة", anchor=False, text_alignment="right")
    with st.container(border=True):
        st.subheader("الجلسة التجريبية والخصوصية", icon=":material/person:", anchor=False, text_alignment="right")
        st.text("العميل التجريبي المحدد: " + customer, text_alignment="right")
        st.text("وضع التشغيل: " + ("محاكاة آمنة" if mode == "Demo" else "تشغيل محلي مباشر"), text_alignment="right")
        st.caption("العميل التجريبي ثابت لهذه الجلسة، ولا يمكن اختيار عميل آخر من الواجهة. هذا ليس حسابًا مصادقًا.", text_alignment="right")
    with st.container(border=True):
        st.subheader("تفضيلات العرض", icon=":material/tune:", anchor=False, text_alignment="right")
        st.text("يمكنك إظهار النشاط والتكلفة، وإتاحة نسخ الإجابة، من «إعدادات العرض» في القائمة الجانبية.", text_alignment="right")
        st.caption("لتبديل المظهر، افتح قائمة التطبيق أعلى الشاشة واختر المظهر الفاتح أو الداكن.", text_alignment="right")
        st.caption("هذه الواجهة للتجربة الآمنة؛ لا تتيح تسجيل دخول أو لوحة مشغل لتفعيل OpenAI المدفوع.", text_alignment="right")
