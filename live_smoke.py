"""One explicitly authorized paid flow. Import/help never sends network requests."""

import argparse
import asyncio
from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from openai_transport import create_async_openai_client as AsyncOpenAI

from agent import Agent
from audit_logger import AuditLogger
from config import AgentConfig, ROOT
from database import init_database, seed_demo_data
from mcp_transport import TrustedSession, mcp_connection


async def check():
    original = AgentConfig.from_env()
    config = replace(original, max_turns=min(original.max_turns, 2),
                     max_output_tokens=min(original.max_output_tokens, 128), max_retries=0,
                     budget_usd=min(original.budget_usd, Decimal("0.005")))
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("API credential required")
    audit = AuditLogger(ROOT / "logs" / ("live_smoke_" + uuid4().hex + ".jsonl"))
    context = TrustedSession("demo_customer_001", "authorized-live-smoke")
    with TemporaryDirectory(prefix="smartsupport-live-") as temporary:
        db = Path(temporary) / "isolated.sqlite3"
        init_database(db)
        seed_demo_data(db)
        async with AsyncOpenAI(max_retries=0, timeout=config.timeout_seconds) as client:
            async with mcp_connection(db, customer_id=context.customer_id, session_id=context.session_id) as session:
                result = await Agent(client, session, context, config, audit=audit).run(
                    "استخدم search_faq فقط لمعرفة مدة الشحن، ثم أعطني إجابة نهائية مختصرة بالعربية.")
    cached = [e["cached_tokens"] for e in audit.events if e["event_type"] == "model_result"]
    report = {"model": config.model, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
              "cached_tokens": sum(cached) if cached and all(n is not None for n in cached) else None,
              "cost_summary": result.cost_summary, "tools_used": result.tools_used,
              "stop_reason": result.canonical_stop_reason,
              "mcp_verified": any(e["event_type"] == "mcp_result" and e["status"] == "success" for e in audit.events)}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return result.stop_reason == "final_answer" and report["mcp_verified"]


def main():
    parser = argparse.ArgumentParser(description="Optional PAID OpenAI/MCP smoke flow, at most 2 model requests")
    parser.add_argument("--allow-paid", action="store_true", help="Explicit operator authorization to spend API credit")
    args = parser.parse_args()
    if not args.allow_paid:
        parser.error("Paid calls are disabled. Explicit --allow-paid authorization is required.")
    try:
        success = asyncio.run(check())
    except Exception:
        print("Live check failed; inspect operator configuration and redacted audit. No sensitive details displayed.")
        raise SystemExit(1)
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
