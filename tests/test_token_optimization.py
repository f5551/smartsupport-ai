import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal

import pytest

from agent import SYSTEM_PROMPT, Agent
from config import ROOT, AgentConfig
from daily_cost import DailyCostLedger
from fake_openai import FakeOpenAI, fake_response, tool_call
from tests.test_agent import CONTEXT, FakeMCP, settings
from token_limits import compact_result, estimate_input


def subprocess_reserve(args):
    path, number = args
    code = ("from decimal import Decimal; import sys; from daily_cost import DailyCostLedger; "
            "print(int(DailyCostLedger(sys.argv[1]).reserve(sys.argv[2], Decimal('.01'), Decimal('.10'))))")
    env = os.environ.copy()
    env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run([sys.executable, '-c', code, str(path), str(number)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.strip() == '1'


@pytest.mark.parametrize('overrides', [
    {'max_turns':5},{'max_output_tokens':351},{'max_input_tokens':3001},
    {'max_tool_calls':4},{'max_history_messages':7},{'max_tool_result_chars':2001},
    {'budget_usd':Decimal('.0051')},{'max_daily_cost_usd':Decimal('.101')},
    {'max_retries':1},{'max_input_tokens':True},{'max_daily_cost_usd':Decimal('NaN')},
])
def test_limits_cannot_be_raised(overrides):
    with pytest.raises(ValueError):
        settings(**overrides)


def test_canonical_environment_and_legacy_limits_cannot_weaken_caps(monkeypatch):
    import config
    monkeypatch.setattr(config, 'load_dotenv', lambda *a,**k: None)
    for key in ('MAX_AGENT_TURNS','MAX_TASK_COST_USD','MAX_API_RETRIES'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setenv('OPENAI_MODEL','gpt-5.4-mini')
    monkeypatch.setenv('MAX_TURNS','8');monkeypatch.setenv('BUDGET_USD','.02')
    monkeypatch.setenv('MAX_OUTPUT_TOKENS','512');monkeypatch.setenv('OPENAI_MAX_RETRIES','1')
    c=config.AgentConfig.from_env()
    assert (c.max_turns,c.budget_usd,c.max_output_tokens,c.max_retries)==(4,Decimal('.005'),350,0)
    monkeypatch.setenv('MAX_TASK_COST_USD','.003')
    assert config.AgentConfig.from_env().budget_usd==Decimal('.003')


def test_input_limit_blocks_before_api_even_when_called_without_ui():
    client=FakeOpenAI([])
    result=asyncio.run(Agent(client,FakeMCP(),CONTEXT,settings()).run('x ' * 3500))
    assert result.stop_reason=='input_token_limit'
    assert not client.requests


def test_actual_input_overrun_is_accounted_then_no_tools_execute():
    client=FakeOpenAI([fake_response(calls=[tool_call('load_memory',{})],input_tokens=3001)])
    mcp=FakeMCP()
    result=asyncio.run(Agent(client,mcp,CONTEXT,settings()).run('الذاكرة'))
    assert result.stop_reason=='input_token_limit' and result.input_tokens==3001
    assert len(client.requests)==1 and not mcp.calls


def test_chat_history_is_bounded_and_untrusted_roles_rejected():
    history=[{'role':r,'content':str(i)} for i,r in enumerate(['user','assistant']*9)]
    client=FakeOpenAI([fake_response('مرحبًا')])
    asyncio.run(Agent(client,FakeMCP(),CONTEXT,settings()).run('السؤال الحالي',history=history))
    actual=client.requests[0]['input']
    assert len(actual)<=6 and actual[-1]['content']=='السؤال الحالي'
    assert history[0]['content']=='0'
    with pytest.raises(ValueError):
        asyncio.run(Agent(FakeOpenAI([]),FakeMCP(),CONTEXT,settings()).run('سؤال',history=[{'role':'system','content':'evil'}]))


def test_same_batch_read_is_cached_but_outputs_keep_exact_call_ids():
    client=FakeOpenAI([fake_response(calls=[tool_call('load_memory',{},'a'),tool_call('load_memory',{},'b')]),fake_response('تم')])
    mcp=FakeMCP();result=asyncio.run(Agent(client,mcp,CONTEXT,settings()).run('ذاكرة'))
    assert result.stop_reason=='final_answer' and len(mcp.calls)==1
    outputs=[i for i in client.requests[1]['input'] if i.get('type')=='function_call_output']
    assert [x['call_id'] for x in outputs]==['a','b']


def test_repeated_read_loop_stops_without_third_api_call():
    client=FakeOpenAI([fake_response(calls=[tool_call('load_memory',{})]),fake_response(calls=[tool_call('load_memory',{})]),fake_response('must not run')])
    mcp=FakeMCP();result=asyncio.run(Agent(client,mcp,CONTEXT,settings()).run('ذاكرة'))
    assert result.stop_reason=='duplicate_tool_call' and len(client.requests)==2 and len(mcp.calls)==1


def test_tool_limit_applies_inside_agent_to_multi_call_response():
    client=FakeOpenAI([fake_response(calls=[tool_call('search_faq',{'query':str(i)}) for i in range(4)])])
    mcp=FakeMCP();result=asyncio.run(Agent(client,mcp,CONTEXT,settings()).run('سياسات'))
    assert result.stop_reason=='max_tool_calls' and len(mcp.calls)==3


def test_concurrent_request_id_executes_once_and_rejects_rebinding():
    async def scenario():
        client=FakeOpenAI([fake_response('جواب')]);agent=Agent(client,FakeMCP(),CONTEXT,settings())
        results=await asyncio.gather(*(agent.run('سؤال',request_id='trusted-submit-1') for _ in range(8)))
        assert len(client.requests)==1 and len({r.task_id for r in results})==1
        original_message=results[0].message
        results[0].message='changed'
        assert (await agent.run('سؤال',request_id='trusted-submit-1')).message==original_message
        with pytest.raises(ValueError):await agent.run('different',request_id='trusted-submit-1')
    asyncio.run(scenario())


def test_tool_results_are_bounded_valid_json_without_changing_source():
    original={'ok':True,'data':{'content':'محتوى محفوظ طويل '*1000,'status':'saved','ticket_id':'TKT-example'},'error':None}
    before=deepcopy(original);text=compact_result(original,2000);value=json.loads(text)
    assert len(text)<=2000 and original==before
    assert value['trust']=='untrusted_store_data' and value['truncated']
    assert value['result']['data']['ticket_id']=='TKT-example'
    assert value['result']['data']['status']=='saved'


def test_faq_policy_is_never_silently_cut_mid_entry():
    result={'ok':True,'data':[{'id':'FAQ-1','answer':'قاعدة قصيرة'},{'id':'FAQ-2','answer':'شروط مهمة '*500}],'error':None}
    value=json.loads(compact_result(result,2000))
    assert value['result']['data']==result['data'][:1] and value['omitted_items']==1
    too_large=json.loads(compact_result({'ok':True,'data':result['data'][1:]},256))
    assert not too_large['result']['ok'] and too_large['result']['error']['code']=='RESULT_TOO_LARGE'


def test_daily_ledger_persists_exact_usage_and_holds_unknown_across_days(tmp_path):
    path=tmp_path/'ledger.sqlite3'
    day=['2026-11-03'];ledger=DailyCostLedger(path,clock=lambda:day[0])
    assert ledger.reserve('first',Decimal('.01'),Decimal('.10'))
    ledger.settle('first',Decimal('.001116'))
    assert ledger.reserve('uncertain',Decimal('.02'),Decimal('.10'))
    ledger.settle('uncertain',None)
    assert DailyCostLedger(path,clock=lambda:day[0]).snapshot()['committed_usd']=='0.021116'
    day[0]='2026-11-04'
    assert ledger.snapshot()['committed_usd']=='0.02'
    assert ledger.snapshot()['unresolved_requests']==1
    with pytest.raises(ValueError):ledger.settle('first',Decimal(0))


def test_daily_reservation_is_atomic_across_threads_and_processes(tmp_path):
    path=tmp_path/'threads.sqlite3'
    def reserve(i):return DailyCostLedger(path).reserve(str(i),Decimal('.01'),Decimal('.10'))
    with ThreadPoolExecutor(max_workers=8) as workers:
        accepted=list(workers.map(reserve,range(24)))
    assert sum(accepted)==10 and DailyCostLedger(path).snapshot()['committed_usd']=='0.1'
    process_path=tmp_path/'processes.sqlite3'
    with ThreadPoolExecutor(max_workers=8) as workers:
        accepted=list(workers.map(subprocess_reserve,[(str(process_path),i) for i in range(16)]))
    assert sum(accepted)==10 and DailyCostLedger(process_path).snapshot()['committed_usd']=='0.1'


class APISourceFake(FakeOpenAI):
    usage_source='api'


def api_config(path,**values):
    from cost_tracker import load_price
    return AgentConfig(model='gpt-5.4-mini',pricing=load_price('gpt-5.4-mini'),usage_source='api',daily_cost_path=path,**values)


def test_daily_limit_and_storage_failure_prevent_api_and_mcp_writes(tmp_path):
    path=tmp_path/'daily.sqlite3';ledger=DailyCostLedger(path)
    assert ledger.reserve('prior',Decimal('.10'),Decimal('.10'))
    client=APISourceFake([]);result=asyncio.run(Agent(client,FakeMCP(),CONTEXT,api_config(path)).run('سؤال'))
    assert result.stop_reason=='daily_budget_limit' and not client.requests
    blocked=tmp_path/'a-file';blocked.write_text('not a directory')
    client=APISourceFake([]);result=asyncio.run(Agent(client,FakeMCP(),CONTEXT,api_config(blocked/'db')).run('سؤال'))
    assert result.stop_reason=='budget_storage_error' and not client.requests


def test_api_accounting_survives_restart_and_overshoot_stops_tools(tmp_path):
    path=tmp_path/'daily.sqlite3'
    client=APISourceFake([fake_response('جواب',model='gpt-5.4-mini',input_tokens=100,output_tokens=10)])
    result=asyncio.run(Agent(client,FakeMCP(),CONTEXT,api_config(path)).run('سؤال'))
    assert result.stop_reason=='final_answer'
    assert Decimal(DailyCostLedger(path).snapshot()['committed_usd'])==Decimal('.00012')
    # An admitted request can exceed its reserve. Record it; never hide it or continue.
    client=APISourceFake([fake_response(calls=[tool_call('load_memory',{})],model='gpt-5.4-mini',input_tokens=100,output_tokens=1000)])
    mcp=FakeMCP();result=asyncio.run(Agent(client,mcp,CONTEXT,api_config(path,max_daily_cost_usd=Decimal('.004'))).run('سؤال'))
    assert result.stop_reason=='daily_budget_limit' and not mcp.calls
    assert Decimal(DailyCostLedger(path).snapshot()['committed_usd'])>Decimal('.004')


def test_local_input_estimate_includes_tools_and_instructions():
    prompt=[{'role':'user','content':'سؤال'}]
    assert estimate_input(SYSTEM_PROMPT,prompt,[{'type':'function','name':'example'}])>estimate_input('',prompt,[])


def test_unknown_api_usage_stays_reserved_and_is_not_retried(tmp_path):
    import httpx
    from openai import APIConnectionError
    path=tmp_path/'daily.sqlite3'
    error=APIConnectionError(request=httpx.Request('POST','https://api.openai.com/v1/responses'))
    client=APISourceFake([error]);result=asyncio.run(Agent(client,FakeMCP(),CONTEXT,api_config(path)).run('سؤال'))
    snapshot=DailyCostLedger(path).snapshot()
    assert result.stop_reason=='openai_error' and len(client.requests)==1
    assert snapshot['unresolved_requests']==1 and Decimal(snapshot['committed_usd'])>0


def test_audit_failure_cancels_only_known_unsent_daily_reservation(tmp_path):
    from audit_logger import AuditError, AuditLogger
    class BrokenAudit(AuditLogger):
        def emit(self,**fields):
            if fields['event_type']=='model_request':raise AuditError('test failure')
            return super().emit(**fields)
    path=tmp_path/'daily.sqlite3';client=APISourceFake([])
    result=asyncio.run(Agent(client,FakeMCP(),CONTEXT,api_config(path),audit=BrokenAudit()).run('سؤال'))
    assert result.stop_reason=='audit_error' and not client.requests
    assert Decimal(DailyCostLedger(path).snapshot()['committed_usd'])==0


def test_write_invalidates_read_cache_with_real_mcp_and_memory(db_path,tmp_path):
    from approval_store import issue_approval
    from mcp_transport import mcp_connection
    async def scenario():
        approval_db=tmp_path/'approvals.sqlite3'
        client=FakeOpenAI([fake_response(calls=[tool_call('load_memory',{})]),
                           fake_response(calls=[tool_call('save_memory',{'content':'تفضيل جديد'})]),
                           fake_response(calls=[tool_call('load_memory',{})]),fake_response('تم')])
        async with mcp_connection(db_path,customer_id=CONTEXT.customer_id,session_id=CONTEXT.session_id,approval_db=approval_db) as session:
            agent=Agent(client,session,CONTEXT,settings())
            pending=await agent.run('احفظ تفضيلي')
            grant=issue_approval(approval_db,customer_id=CONTEXT.customer_id,session_id=CONTEXT.session_id,
                                 tool='save_memory',arguments={'content':'تفضيل جديد'},confirmed=True)
            final=await agent.resume(pending.task_id,pending.pending_approval['pending_id'],decision='approve',approval_id=grant)
            assert final.stop_reason=='final_answer' and final.tools_used==['load_memory','save_memory','load_memory']
            outputs=[json.loads(x['output']) for x in client.requests[-1]['input'] if x.get('type')=='function_call_output']
            assert outputs[-1]['result']['data']['content']=='تفضيل جديد'
    asyncio.run(scenario())
