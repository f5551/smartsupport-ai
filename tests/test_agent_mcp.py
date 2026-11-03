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
    approvals = tmp_path / "approvals.sqlite3"
    client = FakeOpenAI([
        fake_response(calls=[tool_call("save_memory", {"content": "يفضل التواصل بالعربية"})]),
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
        reader = FakeOpenAI([fake_response(calls=[tool_call("load_memory", {})]), fake_response("السياق متاح")])
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id) as session:
            result = await Agent(reader, session, CONTEXT, config()).run("ما تفضيلي المحفوظ؟")
            assert result.stop_reason == "final_answer" and result.tools_used == ["load_memory"]
            assert "يفضل التواصل بالعربية" in str(reader.requests)
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


def test_model_cannot_read_other_customer_even_without_forged_identity(db_path):
    client = FakeOpenAI([fake_response(calls=[tool_call("get_order_status", {"order_id": "DEMO-2001"})]), fake_response("الطلب غير متاح")])
    async def scenario():
        async with mcp_connection(db_path, customer_id=CONTEXT.customer_id, session_id=CONTEXT.session_id) as session:
            result = await Agent(client, session, CONTEXT, config()).run("طلب عميل آخر")
            assert result.stop_reason == "final_answer"
    asyncio.run(scenario())
    assert "ACCESS_DENIED" in str(client.requests[1])
