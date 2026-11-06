"""Archived acceptance tooling must prepare offline from an unrelated CWD."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "reports/evidence/task08_20261105_6c453a57ee85"


@pytest.mark.parametrize("arguments", [[], ["--preflight"]])
def test_task08_preflight_blocks_network_config_clients_and_ledgers(tmp_path, arguments):
    script = """
import json, runpy, socket, sys
root, runner, arguments = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, root)
def forbidden(*args, **kwargs):
    raise AssertionError('Offline preflight attempted a side effect')
socket.socket.connect = forbidden
socket.create_connection = forbidden
import config, openai_transport, daily_cost
config.AgentConfig.from_env = forbidden
openai_transport.create_async_openai_client = forbidden
daily_cost.DailyCostLedger = forbidden
sys.argv = [runner, *arguments]
runpy.run_path(runner, run_name='__main__')
"""
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "RUN_LIVE_OPENAI": "0"}
    environment.pop("OPENAI_API_KEY", None)
    before = list(tmp_path.iterdir())
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT), str(EVIDENCE / "runner.py"), json.dumps(arguments)],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "PASS" and all(report["checks"].values())
    assert report["api_requests"] == 0 and report["mode"] == "offline_preflight"
    assert list(tmp_path.iterdir()) == before


def test_task08_guard_imports_independently_from_unrelated_cwd(tmp_path):
    script = "import runpy,sys; values=runpy.run_path(sys.argv[1]); assert all(n in values for n in ('CountedClient','RunBudget','ScopedMCP'))"
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(EVIDENCE / "guard.py")],
        cwd=tmp_path, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_task08_memory_scenario_does_not_supply_answer_in_history(tmp_path):
    script = """
import json, runpy, sys
values=runpy.run_path(sys.argv[1])
spec=values['memory_followup_spec']()
assert 'history' not in spec
assert values['PREFERENCE'] not in spec['message'] and 'عرب' not in spec['message']
print(json.dumps({'tool':spec['tool'],'history_absent':True}))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(EVIDENCE / "runner.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"tool": "load_memory", "history_absent": True}
