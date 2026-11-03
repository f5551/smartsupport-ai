"""Interactive, deterministic FakeOpenAI. Data answers come ONLY from tool results.

This is a demo response generator, not another agent loop or real language model.
The existing Agent validates and executes every proposed call through real MCP.
"""

import json
import re

from fake_openai import FakeOpenAI, fake_response, tool_call


class DemoOpenAI(FakeOpenAI):
    def __init__(self):
        super().__init__([])

    async def create(self, **kwargs):
        self.script = iter([self.respond])
        return await super().create(**kwargs)

    def respond(self, request):
        outputs = [item for item in request["input"] if item.get("type") == "function_call_output"]
        if outputs:
            envelope = json.loads(outputs[-1]["output"])
            payload = envelope["result"]
            if not payload.get("ok"):
                code = (payload.get("error") or {}).get("code")
                return fake_response("أُلغيت العملية؛ لم تُنفذ الكتابة." if code == "USER_DECLINED" else
                                     "المعلومة غير متاحة أو غير مصرح بها؛ لم أؤكد نجاح أي عملية.")
            data = payload.get("data")
            if isinstance(data, list):
                text = "\n\n".join(f"{row['answer']} (المصدر: {row['id']})" for row in data)
                return fake_response(text or "لا توجد معلومة مطابقة في قاعدة المعرفة التجريبية.")
            if data is None:
                return fake_response("لا يوجد ملخص محفوظ لهذه المحادثة.")
            if data.get("saved"):
                return fake_response("حُفظ الملخص بعد موافقتك، ويمكن استرجاعه في هذه المحادثة.")
            if "content" in data:
                label = "مقتطف من الملخص؛ توجد أجزاء محذوفة:\n" if envelope.get("truncated") else "الملخص المحفوظ لهذه المحادثة:\n"
                return fake_response(label + data["content"])
            statuses = {"processing": "قيد التجهيز", "shipped": "قيد الشحن", "delivered": "تم التسليم",
                        "cancelled": "ملغي", "open": "مفتوحة", "in_progress": "قيد المعالجة",
                        "resolved": "تمت المعالجة", "closed": "مغلقة"}
            if "ticket_id" in data:
                return fake_response(f"التذكرة {data['ticket_id']}؛ الحالة: {statuses.get(data['status'], data['status'])}.")
            return fake_response(f"الطلب {data['order_id']}؛ الحالة: {statuses.get(data['status'], data['status'])}.")
        text = next(item["content"] for item in reversed(request["input"]) if item.get("role") == "user")
        text = text.split("\nالرسالة الحالية:\n")[-1]
        if text.startswith("احفظ هذا الملخص بعد موافقتي:"):
            return fake_response(calls=[tool_call("save_memory", {"content": text.split(":", 1)[1].strip()})])
        if "استرجع الملخص" in text:
            return fake_response(calls=[tool_call("load_memory", {})])
        ticket = re.search(r"(?:DEMO-TKT-\d{3}|TKT-[a-f0-9]{32})", text, re.I)
        if ticket:
            return fake_response(calls=[tool_call("get_ticket_status", {"ticket_id": ticket[0]})])
        order = re.search(r"DEMO-\d{4}", text, re.I)
        if "شكوى" in text or "شكوة" in text:
            if not order:
                return fake_response("اذكر رقم الطلب التجريبي ووصف المشكلة لأجهز شكوى للمراجعة.")
            return fake_response(calls=[tool_call("create_ticket", {"order_id": order[0].upper(), "description": text[:1000]})])
        if order:
            return fake_response(calls=[tool_call("get_order_status", {"order_id": order[0].upper()})])
        if text.strip() in {"مرحبا", "مرحبًا", "مرحبا بك", "السلام عليكم"}:
            return fake_response("مرحبًا بك. أستطيع مساعدتك في الشحن والضمان والطلبات والشكاوى التجريبية.")
        return fake_response(calls=[tool_call("search_faq", {"query": text[:1000]})])
