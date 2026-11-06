"""Native components. Dynamic content is text, never HTML or reasoning."""
import streamlit as st
from datetime import datetime
from decimal import Decimal, InvalidOperation
from web_runtime import redact

STOP_LABELS = {"final_answer": "اكتملت الإجابة", "pending_approval": "بانتظار موافقتك",
               "max_turns": "بلغ حد الجولات", "budget_limit": "بلغ الحد التقديري للميزانية",
               "mcp_error": "تعذر اتصال الأدوات", "openai_error": "تعذر طلب النموذج",
               "session_lost": "فُقدت حالة الجلسة"}


def header(mode, customer, mcp_status):
    with st.container(key="brand"):
        st.title("SmartSupport AI", icon=":material/support_agent:", anchor=False, text_alignment="right")
        st.caption("متجر الإلكترونيات · مساعد خدمة العملاء الذكي · مساحة خاصة بحسابك", text_alignment="right")
        with st.container(horizontal=True, horizontal_alignment="left", vertical_alignment="center"):
            st.badge("محاكاة آمنة" if mode == "Demo" else "تشغيل مباشر محلي", color="blue", icon=":material/science:")
            statuses = {"verified": ("MCP • تم التحقق", "green"), "error": ("MCP • تعذر الاتصال", "red"),
                        "archived": ("MCP • محادثة مؤرشفة", "gray")}
            label, color = statuses.get(mcp_status, ("MCP • لم يُتحقق بعد", "gray"))
            st.badge(label, color=color, icon=":material/hub:")
            st.badge(customer, color="gray", icon=":material/person:")
    st.caption("وضع المحاكاة يستخدم MCP فعليًا وبيانات متجر اصطناعي؛ لا توجد تكلفة OpenAI."
               if mode == "Demo" else "تشغيل مباشر محلي: تُرسل الرسائل إلى OpenAI وقد تترتب تكلفة. لا ترسل بيانات حساسة.", text_alignment="right")


def suggestions(customer_index):
    with st.container(border=True, key="welcome"):
        st.subheader("كيف أقدر أساعدك اليوم؟", anchor=False, text_alignment="right")
        st.text("من أول سؤال إلى متابعة طلبك؛ دعم واضح، وكل كتابة بموافقتك.", text_alignment="right")
        choices = [("ما سياسة الاسترجاع؟", "ما سياسة الاسترجاع؟", "assignment_return"),
                   ("كم تستغرق مدة الشحن؟", "كم تستغرق مدة الشحن؟", "local_shipping"),
                   ("تتبع طلبي", f"ما حالة الطلب DEMO-{customer_index}001؟", "package_2"),
                   ("أجهّز شكوى", f"أريد إنشاء شكوى عن عطل في المنتج للطلب DEMO-{customer_index}002", "support_agent")]
        prompt = None
        for column, (label, question, icon) in zip(st.columns(2), choices[:2]):
            with column:
                if st.button(label, key=f"suggest_{icon}", icon=f":material/{icon}:", width="stretch", wrap=True):
                    prompt = question
        with st.container(horizontal=True, horizontal_alignment="left"):
            for label, question, icon in choices[2:]:
                if st.button(label, key=f"suggest_{icon}", icon=f":material/{icon}:", wrap=True):
                    prompt = question
        st.caption("ابدأ بسؤال جاهز أو اكتب سؤالك بالأسفل.", text_alignment="right")
        return prompt


def chat(messages, *, copy_answer=False, result=None):
    with st.container(border=True, key="chat_surface", height=430 if messages else 230, autoscroll=True):
        st.caption("المحادثة", text_alignment="right")
        if not messages:
            with st.chat_message("assistant", avatar=":material/support_agent:"):
                st.text("أهلًا بك! أساعدك في الشحن والاسترجاع والضمان ومتابعة طلبك التجريبي.", text_alignment="right")
        for message in messages:
            role = message["role"]
            with st.chat_message(role, avatar=":material/support_agent:" if role == "assistant" else ":material/person:"):
                st.caption("رد الوكيل الذكي" if role == "assistant" else "أنت", text_alignment="right")
                st.text(redact(message["content"]), text_alignment="right", width="stretch")
        latest = next((m["content"] for m in reversed(messages) if m["role"] == "assistant"), None)
        if latest and result and result.get("stop_reason") == "final_answer":
            sources = {"search_faq": "قاعدة معرفة المتجر", "get_order_status": "سجل الطلبات",
                       "get_ticket_status": "سجل الشكاوى", "create_ticket": "نتيجة إنشاء الشكوى",
                       "load_memory": "ذاكرة هذه الجلسة", "save_memory": "نتيجة حفظ الذاكرة"}
            successful = list(dict.fromkeys(e["tool_name"] for e in result.get("activity", [])
                                           if e.get("status") == "success" and e.get("tool_name") in sources))
            if successful:
                with st.container(border=True, key="latest_source"):
                    st.caption(":material/database: مصدر الاستعلام عبر MCP", text_alignment="right")
                    for tool in successful:
                        st.caption(sources[tool] + " · " + tool, text_alignment="right")
        if latest and copy_answer:
            with st.expander("نسخ آخر إجابة", icon=":material/content_copy:"):
                st.caption("استخدم زر النسخ في مربع النص التالي.", text_alignment="right")
                st.code(redact(latest), language=None, wrap_lines=True)


def insights(result, model=None):
    if not result:
        with st.container(border=True, key="activity_panel"):
            st.subheader("نشاط الوكيل", anchor=False, text_alignment="right")
            st.caption("لم تُنفذ مهمة بعد. ستظهر هنا استدعاءات الأدوات ونتائجها الفعلية.", text_alignment="right")
            st.caption(":material/lock: موافقة بشرية قبل كل كتابة", text_alignment="right")
        with st.container(border=True, key="cost_panel"):
            st.subheader("الاستهلاك والتكلفة", anchor=False, text_alignment="right")
            st.caption("لم يُستدعَ نموذج بعد. تُعرض القيم بعد أول تنفيذ.", text_alignment="right")
            st.caption("النموذج والميزانية: في انتظار بدء المهمة", text_alignment="right")
        return
    with st.container(border=True, key="activity_panel"):
        st.subheader("نشاط الوكيل", anchor=False, text_alignment="right")
        reason = result["stop_reason"]
        st.caption("سبب التوقف: " + STOP_LABELS.get(reason, "توقف التنفيذ") + " · " + reason, text_alignment="right")
        activity = result.get("activity", [])
        st.caption(f"طلبات MCP: {result.get('tool_call_count', len(activity))} · نتائج مؤكدة: {len(activity)}", text_alignment="right")
        if result.get("tool_call_count", len(activity)) > len(activity):
            st.warning("بعض الاستدعاءات لم تصل لها نتيجة مؤكدة؛ لا نفترض نجاحها.")
        if result.get("pending_approval"):
            st.badge("بانتظار موافقة بشرية · لم تُنفذ الكتابة", color="orange", icon=":material/pause_circle:")
        for index, item in enumerate(result.get("activity_timeline", activity), 1):
            duration = f" · {item['duration_ms']} ms" if item.get("duration_ms") is not None else ""
            st.badge("نجاح" if item["status"] == "success" else "فشل / رفض",
                     color="green" if item["status"] == "success" else "red")
            st.text(("نجاح" if item["status"] == "success" else "فشل / رفض") + " · " + item["tool_name"] + duration, text_alignment="right")
            try:
                timestamp = datetime.fromisoformat(item["timestamp"]).strftime("%H:%M:%S UTC")
                st.caption(f"{index:02d} · {timestamp}", text_alignment="right")
            except (KeyError, TypeError, ValueError):
                pass
            if item.get("error_code"):
                st.caption(item["error_code"], text_alignment="right")
        if result.get("execution_ms") is not None:
            st.caption(f"مدة التنفيذ: {result['execution_ms']} ms", help="تستبعد المدة انتظار موافقة المستخدم. الأحداث أعلاه مرتبة زمنيًا.", text_alignment="right")
    with st.container(border=True, key="cost_panel"):
        st.subheader("الاستهلاك والتكلفة", anchor=False, text_alignment="right")
        source = result.get("cost_summary", {}).get("usage_source", "unverified")
        st.badge("بيانات محاكاة" if source == "simulation" else "استهلاك API" if source == "api" else "استهلاك غير متحقق", color="blue")
        for column, label, value in zip(st.columns(2),
                ["رموز الإدخال", "رموز الإخراج"], [result["input_tokens"], result["output_tokens"]]):
            column.metric(label, str(value))
        st.metric("تكلفة محاكاة (USD)" if source == "simulation" else "تكلفة مقدرة (USD)", result["estimated_cost_usd"])
        st.caption("النموذج: " + redact(result.get("model") or model or "لم يُتحقق من النموذج"), text_alignment="right")
        st.caption("usage_source: " + source + " · estimated_cost_usd: " + result["estimated_cost_usd"], text_alignment="right")
        if source == "simulation":
            st.caption("القيم اصطناعية لا تمثل فاتورة OpenAI أو تكلفة API فعلية.", text_alignment="right")
        if not result.get("usage_complete", True):
            st.warning("الاستهلاك غير مكتمل؛ يتضمن الرقم حجزًا تقديريًا.")
        budget_panel(result)


def budget_values(result):
    """Display-only Decimal arithmetic from actual validated execution metadata."""
    try:
        limit = Decimal(result["budget_usd"])
        spent = Decimal(result["estimated_cost_usd"])
        if not limit.is_finite() or not spent.is_finite() or limit <= 0 or spent < 0:
            return None
        return limit, spent, max(Decimal(0), limit - spent)
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None


def budget_panel(result):
    amounts = budget_values(result)
    if amounts is None:
        st.caption("حالة الميزانية غير متاحة لهذه النتيجة.", text_alignment="right")
        return
    limit, spent, remaining = amounts
    st.divider()
    st.caption("ميزانية المهمة التقديرية", text_alignment="right")
    st.progress(float(min(Decimal(1), spent / limit)))
    st.caption(f"المتبقي تقديريًا: {remaining} USD · الحد: {limit} USD", text_alignment="right")
    st.badge("بلغ الحد التقديري" if remaining == 0 else "ضمن الحد التقديري", color="orange" if remaining == 0 else "green")
    st.caption("تقدير وقائي؛ ليس ضمانًا لسقف الفاتورة.", text_alignment="right")


def approval(pending):
    with st.container(border=True, key="approval_card"):
        st.badge("بانتظار موافقتك", icon=":material/schedule:", color="violet")
        st.subheader("موافقة إنشاء تذكرة" if pending["tool"] == "create_ticket" else "موافقة حفظ الذاكرة", anchor=False, text_alignment="right")
        st.caption("لم تُنفذ هذه العملية بعد. التأكيد يخص هذا المحتوى وهذه المحادثة فقط.", text_alignment="right")
        details = pending["arguments"]
        if pending["tool"] == "create_ticket":
            with st.container(border=True):
                st.markdown(":material/person: **ملخص طلب العميل**")
                st.text("وصف الشكوى: " + redact(details["description"]), text_alignment="right")
            with st.container(border=True):
                st.markdown(":material/description: **معاينة التذكرة**")
                st.text("الطلب: " + details["order_id"], text_alignment="right")
                st.caption("طلب دعم مرتبط بطلبك · لا توجد تذكرة جديدة قبل التأكيد")
            label = "تأكيد إنشاء الشكوى"
        else:
            st.text("الملخص المراد حفظه: " + redact(details["content"]), text_alignment="right")
            label = "تأكيد حفظ الذاكرة"
        st.info("لن تُنفذ العملية قبل موافقتك. يمكنك إلغاء الطلب دون أي كتابة.", icon=":material/verified_user:")
        with st.container(horizontal=True, horizontal_alignment="right"):
            yes = st.button(label, key="approve_" + pending["pending_id"], type="primary", icon=":material/check:", wrap=True)
            no = st.button("إلغاء العملية", key="reject_" + pending["pending_id"], icon=":material/close:")
        return "approve" if yes else "reject" if no else None
