"""Reproducible isolated integration runs. OpenAI is ALWAYS simulated here.

Writes actual audit events, never prewritten success/failure records. Existing
artifacts are not overwritten unless the CLI explicitly supplies --overwrite.
"""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agent import Agent
from audit_logger import AuditLogger
from config import AgentConfig
from cost_tracker import load_price
from database import init_database, seed_demo_data
from fake_openai import FakeOpenAI, fake_response, tool_call
from mcp_transport import TrustedSession, mcp_connection

SCENARIOS = ("success_1", "near_failure_recovery", "explicit_failure")
MODEL = "gpt-4.1-mini-2025-04-14"


class InjectedTransportFailure:
    """Deliberate failure before sending a tool request, not a server response."""
    def __init__(self, session):
        self.session = session

    async def list_tools(self):
        return await self.session.list_tools()

    async def call_tool(self, name, arguments):
        raise ConnectionError("Deliberately injected client transport failure")


async def run_scenario(name, output_dir, *, overwrite=False):
    if name not in SCENARIOS:
        raise ValueError("Unknown scenario")
    output = Path(output_dir) / (name + ".jsonl")
    if output.exists() and not overwrite:
        raise FileExistsError("Trace exists; use a new directory or explicit --overwrite")
    price = load_price(MODEL)
    config = AgentConfig(model=MODEL, pricing=price, input_price=price.input,
                         output_price=price.output, cached_input_price=price.cached_input,
                         usage_source="simulation")
    context = TrustedSession("demo_customer_001", "isolated-trace-session")
    def reply(text="", **kwargs):
        return fake_response(text, model=MODEL, **kwargs)
    if name == "success_1":
        script = [reply(calls=[tool_call("search_faq", {"query": "مدة الشحن"})]),
                  reply("الشحن التجريبي 2–5 أيام عمل وفق FAQ-001.", cached_tokens=10)]
    elif name == "near_failure_recovery":
        # Real MCP denies a foreign order, then the simulated model corrects it.
        script = [reply(calls=[tool_call("get_order_status", {"order_id": "DEMO-2001"})]),
                  reply(calls=[tool_call("get_order_status", {"order_id": "DEMO-1001"})], cached_tokens=10),
                  reply("الطلب المصرح به قيد الشحن؛ لم تُكشف بيانات الطلب الآخر.", cached_tokens=10)]
    else:
        script = [reply(calls=[tool_call("get_order_status", {"order_id": "DEMO-1001"})])]
    with TemporaryDirectory(prefix="smartsupport-trace-") as temporary:
        path = Path(temporary)
        db = path / "isolated.sqlite3"
        init_database(db)
        seed_demo_data(db)
        log = AuditLogger(path / "events.jsonl", source="isolated_integration", scenario=name,
                          fault_source="injected_mcp_transport_failure" if name == "explicit_failure" else "none")
        async with mcp_connection(db, customer_id=context.customer_id, session_id=context.session_id) as session:
            mcp = InjectedTransportFailure(session) if name == "explicit_failure" else session
            result = await Agent(FakeOpenAI(script), mcp, context, config, audit=log).run("طلب اختبار تكامل اصطناعي")
        expected = "mcp_error" if name == "explicit_failure" else "final_answer"
        if result.stop_reason != expected:
            raise RuntimeError("Scenario did not reach its required real outcome; not exporting")
        content = log.path.read_text(encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if overwrite else "x", encoding="utf-8") as target:
            target.write(content)
        return {"scenario": name, "openai": "simulation", "mcp": "real_stdio_with_injected_client_fault" if name == "explicit_failure" else "real_stdio",
                "stop_reason": result.canonical_stop_reason, "cost_summary": result.cost_summary,
                "event_count": len(log.events), "file": output.name}


def main():
    parser = argparse.ArgumentParser(description="Generate real integration audit traces with SIMULATED OpenAI")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "traces")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    async def run():
        return [await run_scenario(name, args.output_dir, overwrite=args.overwrite) for name in SCENARIOS]
    print(json.dumps(asyncio.run(run()), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
