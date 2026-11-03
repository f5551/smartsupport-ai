"""Explicitly authorized single FAQ task; never retry or overwrite a trace."""
import argparse
import asyncio
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from openai_transport import create_async_openai_client as AsyncOpenAI
from agent import Agent
from audit_logger import AuditLogger
from config import AgentConfig, ROOT
from database import DEFAULT_DB_PATH
from mcp_transport import TrustedSession, mcp_connection

QUESTION = "كم تستغرق مدة الشحن؟"
TRACE = ROOT / "traces/live_success_1.jsonl"
OLD = ("success_1.jsonl", "near_failure_recovery.jsonl", "explicit_failure.jsonl")


def fingerprints():
    return {name: hashlib.sha256((ROOT / "traces" / name).read_bytes()).hexdigest() for name in OLD}


def redact(value):
    text = str(value)
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        text = text.replace(key, "[REDACTED]")
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)


class ObservedClient:
    """Capture safe API evidence while enforcing a two-request maximum."""
    def __init__(self, client):
        self.client, self.calls, self.evidence, self.errors = client, 0, [], []
        self.responses = self

    def with_options(self, **kwargs):
        self.client = self.client.with_options(**kwargs)
        return self

    async def create(self, **kwargs):
        if self.calls >= 2:
            raise RuntimeError("Two-request limit reached")
        self.calls += 1
        kwargs["tool_choice"] = {"type": "function", "name": "search_faq"} if self.calls == 1 else "none"
        kwargs["reasoning"] = {"effort": "none"}
        try:
            response = await self.client.responses.create(**kwargs)
        except Exception as exc:
            self.errors.append({"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None),
                                "message": redact(exc)})
            raise
        self.evidence.append({"response_id": response.id, "model": response.model,
                              "status": response.status, "service_tier": response.service_tier,
                              "function_call_outputs_submitted": sum(item.get("type") == "function_call_output"
                                                                     for item in kwargs.get("input", []) if isinstance(item, dict)),
                              "usage": response.usage.model_dump() if response.usage else None,
                              "output": [item.model_dump(mode="json", exclude_none=True) for item in response.output]})
        return response


class FAQOnlyAgent(Agent):
    async def _safe_drive(self, task):
        task.definitions = [tool for tool in task.definitions if tool["name"] == "search_faq"]
        task.validators = {"search_faq": task.validators["search_faq"]}
        return await super()._safe_drive(task)


async def check():
    original = AgentConfig.from_env()
    config = replace(original, max_turns=min(original.max_turns, 2),
                     max_output_tokens=min(original.max_output_tokens, 256),
                     max_retries=0, budget_usd=min(original.budget_usd, Decimal("0.005")))
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY unavailable")
    before = fingerprints()
    db = DEFAULT_DB_PATH
    db_before = hashlib.sha256(db.read_bytes()).hexdigest()
    TRACE.parent.mkdir(exist_ok=True)
    if TRACE.exists():
        records = [json.loads(line) for line in TRACE.read_text(encoding="utf-8").splitlines()]
        if not records or records[-1].get("stop_reason") != "openai_error":
            raise FileExistsError("Existing trace must be preserved; refusing a paid request")
        TRACE.rename(TRACE.with_name("live_failure_previous_" + uuid4().hex + ".jsonl"))
    attempt_path = TRACE.with_name("live_attempt_" + uuid4().hex + ".jsonl")
    attempt_path.touch(exist_ok=False)
    audit = AuditLogger(attempt_path)
    context = TrustedSession("demo_customer_001", "authorized-single-faq-task")
    async with AsyncOpenAI(max_retries=0, timeout=config.timeout_seconds,
                           base_url="https://api.openai.com/v1") as client:
        observed = ObservedClient(client)
        async with mcp_connection(db, customer_id=context.customer_id, session_id=context.session_id) as session:
            result = await FAQOnlyAgent(observed, session, context, config, audit=audit).run(QUESTION)
    cached = [e["cached_tokens"] for e in audit.events if e["event_type"] == "model_result"]
    mcp_verified = any(e["event_type"] == "mcp_result" and e["tool_name"] == "search_faq"
                       and e["status"] == "success" for e in audit.events)
    tool_output_submitted = any(e["function_call_outputs_submitted"] for e in observed.evidence)
    success = result.stop_reason == "final_answer" and mcp_verified and tool_output_submitted
    final_path = TRACE if success else TRACE.with_name("live_failure_" + uuid4().hex + ".jsonl")
    report = {"event_type": "live_report", "question": QUESTION, "configured_model": config.model,
              "model": observed.evidence[-1]["model"] if observed.evidence else None,
              "trace_path": str(final_path),
              "final_answer": result.message if result.stop_reason == "final_answer" else None,
              "stop_reason": result.canonical_stop_reason, "api_requests": observed.calls,
              "input_tokens": result.input_tokens if cached else None,
              "output_tokens": result.output_tokens if cached else None,
              "cached_tokens": sum(cached) if cached and all(n is not None for n in cached) else None,
              "cost_summary": result.cost_summary, "errors": observed.errors,
              "tools_used": result.tools_used,
              "mcp_verified": mcp_verified, "tool_output_submitted": tool_output_submitted,
              "store_unchanged": db_before == hashlib.sha256(db.read_bytes()).hexdigest(),
              "old_traces_unchanged": before == fingerprints(), "old_trace_sha256": before}
    with attempt_path.open("a", encoding="utf-8") as stream:
        for evidence in observed.evidence:
            stream.write(redact(json.dumps({"event_type": "api_evidence", **evidence}, ensure_ascii=False)) + "\n")
        stream.write(redact(json.dumps(report, ensure_ascii=False)) + "\n")
    attempt_path.rename(final_path)
    print(redact(json.dumps(report, ensure_ascii=True, indent=2)))
    return success and report["mcp_verified"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid", action="store_true")
    if not parser.parse_args().allow_paid:
        parser.error("Explicit --allow-paid required")
    try:
        success = asyncio.run(check())
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": redact(exc)}, ensure_ascii=True))
        raise SystemExit(1)
    raise SystemExit(0 if success else 1)
