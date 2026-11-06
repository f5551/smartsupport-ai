"""Single authorized five-scenario live acceptance run; no retries or production data."""
import asyncio, json, os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from openai_transport import create_async_openai_client
from agent import Agent
from audit_logger import AuditLogger
from config import AgentConfig, ROOT
from database import init_database, seed_demo_data, save_memory
from approval_store import issue_approval
from daily_cost import DailyCostLedger
from mcp_transport import TrustedSession, mcp_connection

TOTAL_CAP = Decimal("0.025")
PRIOR_UNKNOWN = Decimal("0.002445")
TASK_CAP = Decimal("0.003") if os.environ.get("RECOVERY_ONLY") else Decimal("0.0045")

class ForcedClient:
    def __init__(self, client, tool):
        self.client, self.tool, self.calls, self.responses = client, tool, 0, self
        self.evidence, self.errors = [], []
    def with_options(self, **kwargs):
        self.client = self.client.with_options(**kwargs)
        return self
    async def create(self, **kwargs):
        self.calls += 1
        kwargs["tool_choice"] = {"type": "function", "name": self.tool} if self.tool and self.calls == 1 else "none"
        try:
            response = await self.client.responses.create(**kwargs)
        except Exception as exc:
            self.errors.append(type(exc).__name__)
            raise
        usage = response.usage.model_dump() if response.usage else None
        self.evidence.append({"model": response.model, "usage": usage})
        return response

async def one(client, db, approvals, spec, config):
    context = TrustedSession(spec["customer"], spec["session"])
    audit = AuditLogger(ROOT / "logs" / ("live_acceptance_" + spec["id"] + ".jsonl"), source="isolated_integration")
    forced = ForcedClient(client, spec.get("tool"))
    async with mcp_connection(db, customer_id=context.customer_id, session_id=context.session_id,
                              approval_db=approvals) as mcp:
        agent = Agent(forced, mcp, context, config, audit=audit)
        result = await agent.run(spec["message"])
        if spec.get("approve") and result.stop_reason == "pending_approval":
            pending = result.pending_approval
            grant = issue_approval(approvals, customer_id=context.customer_id, session_id=context.session_id,
                                   tool=pending["tool"], arguments=pending["arguments"], confirmed=True)
            result = await agent.resume(result.task_id, pending["pending_id"], decision="approve", approval_id=grant)
    mcp_ok = any(e["event_type"] == "mcp_result" and e["tool_name"] == spec.get("tool") and e["status"] == "success" for e in audit.events)
    cost = result.cost_summary.get("total_cost_usd")
    if cost is None or not result.usage_complete:
        return {"id": spec["id"], "status": "FAIL", "reason": "usage_unknown", "stop_reason": result.canonical_stop_reason}
    return {"id": spec["id"], "status": "PASS" if result.stop_reason == "final_answer" and (not spec.get("tool") or mcp_ok) else "FAIL",
            "stop_reason": result.canonical_stop_reason, "tools": result.tools_used, "mcp_verified": mcp_ok,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cached_tokens": sum(e["cached_tokens"] or 0 for e in audit.events if e["event_type"] == "model_result"),
            "cost_usd": cost, "answer": result.message, "api_requests": forced.calls}

async def main():
    base = AgentConfig.from_env()
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY unavailable")
    config = replace(base, budget_usd=TASK_CAP, max_turns=3, max_output_tokens=128, max_retries=0)
    ledger = DailyCostLedger(config.daily_cost_path)
    before = ledger.snapshot()
    if Decimal(before["committed_usd"]) + (TOTAL_CAP - PRIOR_UNKNOWN) > config.max_daily_cost_usd:
        raise RuntimeError("daily budget cannot cover authorized run")
    scenarios = [
        {"id":"shipping","message":"كم تستغرق مدة الشحن؟","tool":"search_faq","customer":"demo_customer_001","session":"accept-shipping"},
        {"id":"order_status","message":"ما حالة الطلب DEMO-1001؟","tool":"get_order_status","customer":"demo_customer_001","session":"accept-order"},
        {"id":"memory_followup","message":"ما التفضيل المحفوظ لي؟","tool":"load_memory","customer":"demo_customer_001","session":"accept-memory"},
        {"id":"ambiguous","message":"أريد متابعة طلبي الآخر.","tool":None,"customer":"demo_customer_001","session":"accept-ambiguous"},
    ]
    if not os.environ.get("RECOVERY_ONLY"):
        scenarios.insert(2, {"id":"create_ticket","message":"أنشئ تذكرة دعم اصطناعية للطلب DEMO-1002 بسبب تلف تجريبي.","tool":"create_ticket","approve":True,"customer":"demo_customer_001","session":"accept-ticket"})
    else:
        scenarios = [s for s in scenarios if s["id"] in {"shipping", "order_status", "memory_followup"}]
    results=[]
    with TemporaryDirectory(prefix="smartsupport-acceptance-") as td:
        root=Path(td); db=root/"store.sqlite3"; approvals=root/"approvals.sqlite3"
        init_database(db); seed_demo_data(db)
        save_memory("demo_customer_001", "accept-memory", "يفضل العميل التواصل بالعربية", approved=True, db_path=db)
        async with create_async_openai_client(max_retries=0, timeout=config.timeout_seconds, base_url="https://api.openai.com/v1") as client:
            for spec in scenarios:
                current=ledger.snapshot()
                if Decimal(current["committed_usd"]) + TASK_CAP > config.max_daily_cost_usd:
                    results.append({"id":spec["id"],"status":"NOT_EXECUTED","reason":"daily_budget_guard"}); break
                result=await one(client, db, approvals, spec, config); results.append(result)
                if result.get("status") == "FAIL" and result.get("reason") == "usage_unknown": break
    settled=[Decimal(r["cost_usd"]) for r in results if r.get("status")=="PASS" and r.get("cost_usd") is not None]
    report={"results":results,"settled_count":len(settled),"average_settled_cost_usd":str(sum(settled)/len(settled)) if settled else None,
            "ledger_after":ledger.snapshot(),"prior_unknown_usd":str(PRIOR_UNKNOWN),"task_cap_usd":str(TASK_CAP),"total_cap_usd":str(TOTAL_CAP)}
    stem = "live_acceptance_recovery" if os.environ.get("RECOVERY_ONLY") else "live_acceptance_raw"
    (ROOT/"reports"/(stem+"_raw.json")).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    lines=["# Live acceptance — five scenarios (synthetic, 2026-11-04)","","No automatic retries; temporary SQLite databases only.",""]
    for r in results:
        lines.append(f"- **{r['id']}**: **{r['status']}** — tokens in/out {r.get('input_tokens','—')}/{r.get('output_tokens','—')}, cached {r.get('cached_tokens','—')}, cost ${r.get('cost_usd','—')}, stop `{r.get('stop_reason','—')}`, MCP `{r.get('mcp_verified','—')}`.")
    lines += ["",f"Settled tasks: {len(settled)}; average settled cost: ${report['average_settled_cost_usd'] or '—'}.",f"Prior unknown reservation retained: ${PRIOR_UNKNOWN}; ledger after: {report['ledger_after']}."]
    (ROOT/"reports"/(stem+".md")).write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=True))

if __name__ == "__main__":
    asyncio.run(main())
