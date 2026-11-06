import asyncio
import json

import pytest

from demo_client import DemoOpenAI


def ask(text, history=()):
    request={"input":[*history,{"role":"user","content":text}]}
    response=asyncio.run(DemoOpenAI().create(**request))
    calls=[item for item in response.output if item.type=="function_call"]
    answer="".join(part.text for item in response.output if item.type=="message" for part in item.content if part.type=="output_text")
    return calls,answer


@pytest.mark.parametrize("text",["ما حالة طلبي؟","أين طلبي؟","وين طلبي؟","حالة الطلب؟"])
def test_arabic_order_status_without_number_asks_for_identifier(text):
    calls,answer=ask(text)
    assert calls==[] and "رقم الطلب" in answer


@pytest.mark.parametrize("text",["أريد مساعدة","مساعدة","عندي استفسار"])
def test_vague_arabic_request_clarifies_without_irrelevant_faq(text):
    calls,answer=ask(text)
    assert calls==[] and "تحديدًا" in answer


def test_followup_uses_nearest_explicit_prior_order_then_queries_mcp():
    history=[{"role":"user","content":"ما حالة الطلب DEMO-1001؟"},
             {"role":"assistant","content":"الطلب DEMO-1001؛ الحالة: قيد الشحن."}]
    calls,answer=ask("هل تم تسليمه؟",history)
    assert len(calls)==1 and calls[0].name=="get_order_status"
    assert json.loads(calls[0].arguments)=={"order_id":"DEMO-1001"}
    assert answer==""


def test_other_order_reference_clarifies_instead_of_searching_faq():
    calls,answer=ask("وماذا عن طلبي الآخر؟",[
        {"role":"user","content":"ما حالة الطلب DEMO-1002؟"},
        {"role":"assistant","content":"الطلب DEMO-1002؛ الحالة: تم التسليم."},
    ])
    assert calls==[] and "أي طلب" in answer and "رقم الطلب" in answer


def test_final_simulated_status_is_derived_only_from_mcp_result():
    client=DemoOpenAI()
    response=client.respond({"input":[{"type":"function_call_output","output":json.dumps({
        "result":{"ok":True,"data":{"order_id":"DEMO-1001","status":"shipped"},"error":None}
    },ensure_ascii=False)}]})
    answer=response.output_text
    assert "قيد الشحن" in answer and "تم التسليم" not in answer


def test_failed_mcp_status_does_not_fill_status_from_history_or_guess():
    response=DemoOpenAI().respond({"input":[{"type":"function_call_output","output":json.dumps({
        "result":{"ok":False,"data":None,"error":{"code":"ACCESS_DENIED"}}
    },ensure_ascii=False)}]})
    assert "غير متاحة أو غير مصرح بها" in response.output_text
    assert "قيد الشحن" not in response.output_text and "تم التسليم" not in response.output_text
