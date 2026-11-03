import asyncio
import json

import pytest

from generate_traces import SCENARIOS, run_scenario


def test_three_traces_are_generated_from_isolated_execution(tmp_path):
    async def scenario():
        return [await run_scenario(name, tmp_path) for name in SCENARIOS]
    reports = asyncio.run(scenario())
    for report in reports:
        path = tmp_path / report["file"]
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == report["event_count"]
        assert all(e["usage_source"] == "simulation" and e["source"] == "isolated_integration" for e in records)
        assert records[0]["event_type"] == "task_start" and records[-1]["event_type"] == "task_end"
        assert report["cost_summary"]["api_measured_cost_usd"] is None
        if report["scenario"] == "explicit_failure":
            assert records[-1]["stop_reason"] == "mcp_error"
            assert records[-1]["status"] == "stopped"
            assert any(e["event_type"] == "transport_error" and e["fault_source"] == "injected_mcp_transport_failure" for e in records)
        elif report["scenario"] == "near_failure_recovery":
            assert any(e["error_code"] == "ACCESS_DENIED" for e in records)
            assert records[-1]["stop_reason"] == "final_answer"
        else:
            assert records[-1]["stop_reason"] == "final_answer"
    with pytest.raises(FileExistsError):
        asyncio.run(run_scenario("success_1", tmp_path))
