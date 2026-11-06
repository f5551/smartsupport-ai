import asyncio
from decimal import Decimal

import pytest

from agent import Agent
from approval_store import issue_approval
from config import AgentConfig
from fake_openai import FakeOpenAI, fake_response, tool_call
from mcp_transport import TrustedSession, mcp_connection

CONTEXT = TrustedSession("demo_customer_001", "agent-integration")


def config():
    return AgentConfig(model="fake-test-model", input_price=Decimal("0.1"), output_price=Decimal("0.2"))


def test_agent_with_fake_openai_uses_real_mcp_reads(db_path):
    client = FakeOpenAI([
        fake_response(calls=[tool_call("search_faq", {"query": "مدة الشحن"}), tool_call("get_order_status", {"order_id": "DEMO-1001"})]),
        fake_response("الشحن 2–5 أيام عمل؛ طلبك قيد الشحن.")])
    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id) as session:
            result = await Agent(client, session, CONTEXT, config()).run("أين طلبي وكم مدة الشحن؟")
            assert result.stop_reason == "final_answer"
            assert result.tools_used == ["search_faq", "get_order_status"]
    asyncio.run(scenario())
    import json
    returned = [json.loads(item["output"])["result"] for item in client.requests[1]["input"] if item.get("type") == "function_call_output"]
    assert returned[0]["data"][0]["id"] == "FAQ-001"
    assert returned[1]["data"]["status"] == "shipped"


def test_agent_approval_resume_and_memory_persist_across_process_restart(db_path, tmp_path):
    import json
    from uuid import uuid4

    approvals = tmp_path / "approvals.sqlite3"
    preference = "يفضل التواصل بالعربية؛ تفضيل اصطناعي خاص " + uuid4().hex
    client = FakeOpenAI([
        fake_response(calls=[tool_call("save_memory", {"content": preference})]),
        fake_response("تم حفظ الملخص بعد موافقتك.")])
    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id, approval_db=approvals) as session:
            agent = Agent(client, session, CONTEXT, config())
            pending = await agent.run("احفظ تفضيلي")
            assert pending.stop_reason == "pending_approval" and pending.tools_used == []
            public = pending.pending_approval
            # Explicit TEST consent only, in isolated databases.
            reference = issue_approval(approvals, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id,
                tool=public["tool"], arguments=public["arguments"], confirmed=True)
            # Mutating the public result must not change the internal operation.
            public["arguments"]["content"] = "tampered copy"
            final = await agent.resume(pending.task_id, public["pending_id"], decision="approve", approval_id=reference)
            assert final.stop_reason == "final_answer" and final.turns == 2
            assert final.tools_used == ["save_memory"]
            assert reference not in str(client.requests)
            with pytest.raises(ValueError):
                await agent.resume(pending.task_id, public["pending_id"], decision="approve", approval_id=reference)
        def request_memory(request):
            assert preference not in str(request)
            assert "يفضل التواصل بالعربية" not in str(request)
            assert not any(item.get("type") == "function_call_output" for item in request["input"])
            return fake_response(calls=[tool_call("load_memory", {})])

        def inspect_memory_result(request):
            outputs = [json.loads(item["output"])["result"] for item in request["input"]
                       if item.get("type") == "function_call_output"]
            assert len(outputs) == 1 and outputs[0]["ok"] is True
            assert outputs[0]["data"]["content"] == preference
            return fake_response("تمت قراءة نتيجة الأداة.")  # Does not supply the preference.

        reader = FakeOpenAI([request_memory, inspect_memory_result])
        # The saving MCP process has exited above; this starts a new subprocess
        # and a new Agent with no carried conversation history.
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id) as session:
            result = await Agent(reader, session, CONTEXT, config()).run("ما تفضيلي المحفوظ؟", history=())
            assert result.stop_reason == "final_answer" and result.tools_used == ["load_memory"]
            assert preference in result.message
            assert preference not in str(reader.requests[0])
            assert preference in str(reader.requests[1])
            assert "tampered copy" not in str(reader.requests)
    asyncio.run(scenario())


def test_agent_ticket_requires_explicit_test_grant_over_real_mcp(db_path, tmp_path):
    approvals = tmp_path / "ticket-approvals.sqlite3"
    client = FakeOpenAI([fake_response(calls=[tool_call("create_ticket", {"order_id": "DEMO-1002", "description": "تلف تجريبي"})]),
                         fake_response("تمت معالجة نتيجة الأداة.")])
    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id, approval_db=approvals) as session:
            agent = Agent(client, session, CONTEXT, config())
            result = await agent.run("شكوى تجريبية")
            assert result.stop_reason == "pending_approval" and result.tools_used == []
            pending = result.pending_approval
            reference = issue_approval(approvals, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id,
                tool=pending["tool"], arguments=pending["arguments"], confirmed=True)
            result = await agent.resume(result.task_id, pending["pending_id"], decision="approve", approval_id=reference)
            assert result.tools_used == ["create_ticket"] and result.stop_reason == "final_answer"
            assert result.write_receipts[0]["result"]["data"]["ticket_id"].startswith("TKT-")
    asyncio.run(scenario())


def test_successful_ticket_write_returns_mcp_receipt_when_final_round_is_exhausted(db_path, tmp_path):
    approvals = tmp_path / "ticket-max-turns.sqlite3"
    first = fake_response(calls=[tool_call("create_ticket", {"order_id": "DEMO-1002", "description": "تلف تجريبي"}, "ticket-call-1")])
    second = fake_response(calls=[tool_call("create_ticket", {"order_id": "DEMO-1002", "description": "تلف تجريبي"}, "ticket-call-2")])
    client = FakeOpenAI([first, second])
    limited = AgentConfig(model="fake-test-model", input_price=Decimal("0.1"), output_price=Decimal("0.2"), max_turns=2)

    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id,
                                  approval_db=approvals) as session:
            agent = Agent(client, session, CONTEXT, limited)
            pending = await agent.run("شكوى تجريبية")
            grant = issue_approval(approvals, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id,
                                   tool="create_ticket", arguments=pending.pending_approval["arguments"], confirmed=True)
            result = await agent.resume(pending.task_id, pending.pending_approval["pending_id"],
                                        decision="approve", approval_id=grant)
            assert result.stop_reason == "final_answer"
            assert "تم إنشاء التذكرة TKT-" in result.message
            assert len(result.write_receipts) == 1
            assert len([c for c in client.requests if c["model"] == "fake-test-model"]) == 2
    asyncio.run(scenario())


def test_model_cannot_read_other_customer_even_without_forged_identity(db_path):
    client = FakeOpenAI([fake_response(calls=[tool_call("get_order_status", {"order_id": "DEMO-2001"})]), fake_response("الطلب غير متاح")])
    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id) as session:
            result = await Agent(client, session, CONTEXT, config()).run("طلب عميل آخر")
            assert result.stop_reason == "final_answer"
    asyncio.run(scenario())
    assert "ACCESS_DENIED" in str(client.requests[1])


@pytest.mark.parametrize("last_round_calls", [False, True])
def test_order_read_then_ticket_write_returns_both_verified_results_once(db_path, tmp_path, last_round_calls):
    from dataclasses import replace
    from database import connect

    arguments = {"order_id": "DEMO-1002", "description": "اختبار نتيجة التذكرة بعد قراءة الطلب"}
    read = tool_call("get_order_status", {"order_id": "DEMO-1002"})
    write = tool_call("create_ticket", arguments)
    if last_round_calls:
        # Both tools are queued in order; the final model round asks to write
        # again, which must return the existing receipt without another write.
        script = [fake_response(calls=[read, write]),
                  fake_response(calls=[tool_call("create_ticket", arguments)])]
        settings = replace(config(), max_turns=2)
    else:
        script = [fake_response(calls=[read]), fake_response(calls=[write]),
                  fake_response("تم إنشاء TKT-invented والطلب تم تسليمه.")]
        settings = config()
    client = FakeOpenAI(script)
    approvals = tmp_path / "mixed-approvals.sqlite3"

    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id,
                                  session_id=CONTEXT.session_id, approval_db=approvals) as session:
            agent = Agent(client, session, CONTEXT, settings)
            question = "تحقق من الطلب DEMO-1002 ثم أنشئ شكوى بالوصف المعتمد"
            with connect(db_path) as db:
                before = db.execute("SELECT count(*) FROM tickets").fetchone()[0]
                status = db.execute("SELECT status FROM orders WHERE order_id='DEMO-1002'").fetchone()[0]
            pending = await agent.run(question, request_id="mixed-request")
            assert pending.stop_reason == "pending_approval"
            with connect(db_path) as db:
                assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == before
            grant = issue_approval(approvals, customer_id=CONTEXT.customer_id,
                                   session_id=CONTEXT.session_id, tool="create_ticket",
                                   arguments=arguments, confirmed=True)
            final = await agent.resume(pending.task_id, pending.pending_approval["pending_id"],
                                       decision="approve", approval_id=grant)
            assert final.stop_reason == "final_answer"
            assert final.tools_used == ["get_order_status", "create_ticket"]
            assert len(final.write_receipts) == 1
            ticket = final.write_receipts[0]["result"]["data"]["ticket_id"]
            assert final.message.startswith("تم إنشاء التذكرة " + ticket)
            assert "الطلب DEMO-1002؛ الحالة:" in final.message
            assert {"processing": "قيد التجهيز", "shipped": "قيد الشحن", "delivered": "تم التسليم"}[status] in final.message
            assert not any(private in final.message for private in (grant, "write_receipts", "approval_id", "idempotency_key", "TKT-invented"))
            replay = await agent.run(question, request_id="mixed-request")
            assert replay.message == final.message and replay.turns == final.turns
            assert len(client.requests) == (2 if last_round_calls else 3)
            with connect(db_path) as db:
                assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == before + 1
                assert db.execute("SELECT count(*) FROM write_operations WHERE action='create_ticket' AND state='committed'").fetchone()[0] == 1
    asyncio.run(scenario())
