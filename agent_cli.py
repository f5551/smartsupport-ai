"""Local CLI: explicit --live opt-in, or isolated --fake-demo with real MCP."""

import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from openai_transport import create_async_openai_client as AsyncOpenAI

from agent import Agent
from approval_store import issue_approval
from config import AgentConfig, ROOT
from fake_openai import FakeOpenAI, fake_response, tool_call
from mcp_transport import TrustedSession, mcp_connection


def display(result):
    text = json.dumps(result.as_dict(), ensure_ascii=False, indent=2)
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    print(text)


async def interact(client, config, context, db_path, approval_db, message, *, interactive):
    async with mcp_connection(db_path, customer_id=context.customer_id, session_id=context.session_id,
                              approval_db=approval_db) as mcp:
        agent = Agent(client, mcp, context, config)
        result = await agent.run(message)
        display(result)
        while interactive and result.stop_reason == "pending_approval" and approval_db is not None:
            # Displayed pending arguments are exactly what is stored in the grant.
            decision = input("راجع العملية المعروضة. اكتب أوافق للموافقة، أو أي نص آخر للرفض: ").strip()
            pending = result.pending_approval
            approval_id = None
            if decision == "أوافق":
                approval_id = issue_approval(
                    approval_db, customer_id=context.customer_id, session_id=context.session_id,
                    tool=pending["tool"], arguments=pending["arguments"], confirmed=True,
                )
            result = await agent.resume(result.task_id, pending["pending_id"],
                                        decision="approve" if approval_id else "reject", approval_id=approval_id)
            display(result)
        return result


async def live(args):
    config = AgentConfig.from_env()
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Missing API key")
    if not args.demo_customer_id or not args.demo_session_id:
        raise ValueError("Explicit local demo identity required")
    context = TrustedSession(args.demo_customer_id, args.demo_session_id)
    message = args.message or input("رسالة العميل: ")
    async with AsyncOpenAI(max_retries=0, timeout=config.timeout_seconds) as client:
        return await interact(client, config, context, args.db, args.approval_db, message, interactive=True)


def main():
    # Windows redirected terminals may default to cp1252; preserve Arabic safely.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="SmartSupport local demo agent (no production authentication)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Allow paid OpenAI calls with your configured model/prices")
    mode.add_argument("--fake-demo", action="store_true", help="Simulated OpenAI, actual MCP, isolated temporary database")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "smartsupport.db")
    parser.add_argument("--demo-customer-id")
    parser.add_argument("--demo-session-id")
    parser.add_argument("--approval-db", type=Path)
    parser.add_argument("--message")
    args = parser.parse_args()
    try:
        if args.fake_demo:
            # Setup only: the AGENT never uses these database functions.
            from database import init_database, seed_demo_data
            print("محاكاة OpenAI فقط؛ الأدوات تمر عبر خادم MCP حقيقي. لا توجد تكلفة API فعلية.")
            with TemporaryDirectory(prefix="smartsupport-agent-") as temporary:
                path = Path(temporary) / "demo.sqlite3"
                init_database(path)
                seed_demo_data(path)
                client = FakeOpenAI([
                    fake_response(calls=[tool_call("search_faq", {"query": "كم تستغرق مدة الشحن؟"})]),
                    fake_response("مدة الشحن التجريبية 2–5 أيام عمل بعد تأكيد الطلب، وفق FAQ-001."),
                ])
                result = asyncio.run(interact(client, AgentConfig(model="fake-test-model", input_price=Decimal("0.1"),
                    output_price=Decimal("0.2")), TrustedSession("demo_customer_001", "fake-demo"), path, None,
                    "كم تستغرق مدة الشحن؟", interactive=False))
        else:
            result = asyncio.run(live(args))
    except (Exception, KeyboardInterrupt):
        print("تعذر إكمال التجربة. تحقق من الإعدادات والقاعدة والاتصال؛ لم تُعرض تفاصيل حساسة.")
        raise SystemExit(1)
    if result.stop_reason not in {"final_answer", "pending_approval"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
