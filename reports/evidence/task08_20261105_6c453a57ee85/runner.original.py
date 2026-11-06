"""Authorized Task 08. New evidence only; no production Agent/MCP changes."""
import asyncio
from contextlib import asynccontextmanager, closing
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import Agent
from approval_store import issue_approval
from audit_logger import AuditLogger
from config import AgentConfig
from daily_cost import DailyCostLedger
from database import init_database,seed_demo_data
from mcp_transport import TrustedSession,mcp_connection
from openai_transport import create_async_openai_client,ssl_context
from ticket_budget import generation_bound
from task08_guard import CountedClient,RunBudget,ScopedMCP
import pytest

DESCRIPTION='اختبار TASK 08 النهائي — تلف اصطناعي'
PREFERENCE='يفضل العميل التواصل بالعربية'
MODEL='gpt-5.4-mini-2026-03-17'

def write(path,value):
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)

def rows(path,sql,args=()):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        return db.execute(sql,args).fetchall()

def public_result(result,guard,audit,checks):
    known=guard.tracker.known_cost
    complete=not guard.tracker.reservations and not guard.halted
    checks={**checks,'final_answer':result is not None and result.stop_reason=='final_answer',
            'settled_usage':complete and guard.calls>0,'within_turns':result is not None and result.turns<=2,
            'within_budget':known<=Decimal('.005')}
    answer=result.message if result is not None else None
    if answer and ('write_receipts' in answer or os.environ.get('OPENAI_API_KEY','no-key') in answer):
        answer=None;checks['public_answer']=False
    return {'status':'PASS' if all(checks.values()) else 'FAIL','checks':checks,
            'stop_reason':result.canonical_stop_reason if result else None,'answer':answer,
            'tools':result.tools_used if result else [],'input_tokens':guard.tracker.input_tokens,
            'output_tokens':guard.tracker.output_tokens,
            'cached_tokens':sum(r['cached_tokens'] or 0 for r in guard.tracker.records),
            'cost_usd':str(known) if complete else None,'known_cost_usd':str(known),
            'generation_requests':guard.calls,'count_requests':guard.count_calls,
            'provider_usage':guard.evidence,'errors':guard.errors,
            'usage_complete':complete,'audit_events':len(audit.events)}

class LivePlugin:
    def __init__(self,cfg,budget,out):self.cfg,self.budget,self.out=cfg,budget,out;self.result=None
    @pytest.fixture(autouse=True)
    def instrument(self,request,monkeypatch):
        if request.node.name!='test_real_openai_with_real_mcp_opt_in_only':return
        self.audit=AuditLogger(self.out/'shipping.jsonl',source='isolated_integration')
        self.guard=CountedClient(create_async_openai_client(max_retries=0,timeout=self.cfg.timeout_seconds,
            base_url='https://api.openai.com/v1'),self.cfg.pricing,self.budget)
        owner=self
        class ObservedAgent(Agent):
            def __init__(self,*args,**kwargs):
                super().__init__(*args,**kwargs,audit=owner.audit);self.daily=None
            async def run(self,*args,**kwargs):
                owner.result=await super().run(*args,**kwargs);return owner.result
        @asynccontextmanager
        async def scoped(*args,**kwargs):
            async with mcp_connection(*args,**kwargs) as session:
                self.mcp=ScopedMCP(session,{'search_faq'})
                yield self.mcp
        monkeypatch.setattr(request.module,'AsyncOpenAI',lambda **kwargs:self.guard)
        monkeypatch.setattr(request.module,'Agent',ObservedAgent)
        monkeypatch.setattr(request.module,'mcp_connection',scoped)
        monkeypatch.setattr(request.module.AgentConfig,'from_env',classmethod(lambda cls:self.cfg))

async def scenario(spec,store,approvals,cfg,budget,out):
    context=TrustedSession('demo_customer_001',spec['session'])
    audit=AuditLogger(out/(spec['id']+'.jsonl'),source='isolated_integration')
    args={'order_id':'DEMO-1002','description':DESCRIPTION}
    before=rows(store,'SELECT count(*) FROM tickets')[0][0]
    extra={}
    guard=CountedClient(create_async_openai_client(max_retries=0,timeout=cfg.timeout_seconds,
        base_url='https://api.openai.com/v1'),cfg.pricing,budget)
    async with guard:
        async with mcp_connection(store,customer_id=context.customer_id,session_id=context.session_id,approval_db=approvals) as session:
            scoped=ScopedMCP(session,{spec['tool']} if spec['tool'] else set(),args if spec['id']=='create_ticket' else None)
            agent=Agent(guard,scoped,context,cfg,audit=audit);agent.daily=None
            result=await agent.run(spec['message'],history=spec.get('history',()))
            if spec['id']=='create_ticket':
                exact=(result.stop_reason=='pending_approval' and result.pending_approval['tool']=='create_ticket'
                       and result.pending_approval['arguments']==args)
                extra['exact_pending_before_write']=exact
                extra['zero_write_before_approval']=rows(store,'SELECT count(*) FROM tickets')[0][0]==before
                if exact and not guard.halted:
                    pending=result.pending_approval
                    grant=issue_approval(approvals,customer_id=context.customer_id,session_id=context.session_id,
                        tool='create_ticket',arguments=args,confirmed=True)
                    result=await agent.resume(result.task_id,pending['pending_id'],decision='approve',approval_id=grant)
                    tickets=rows(store,'SELECT ticket_id FROM tickets WHERE description=?',(DESCRIPTION,))
                    ops=rows(store,"SELECT result_json FROM write_operations WHERE action='create_ticket' AND state='committed'")
                    extra['one_ticket']=len(tickets)==1 and rows(store,'SELECT count(*) FROM tickets')[0][0]==before+1
                    extra['durable_result']=len(ops)==1 and len(tickets)==1 and json.loads(ops[0][0]).get('ticket_id')==tickets[0][0]
                    extra['ticket_in_answer']=len(tickets)==1 and tickets[0][0] in result.message
                    if extra['durable_result']:
                        replay=await session.call_tool('create_ticket',{**args,'approval_id':grant})
                        extra['idempotent_replay']=not replay.isError and replay.structuredContent['data']['ticket_id']==tickets[0][0] and rows(store,'SELECT count(*) FROM tickets')[0][0]==before+1
                    else:extra['idempotent_replay']=False
    tools=[e['tool_name'] for e in audit.events if e['event_type']=='mcp_result' and e['status']=='success']
    checks={'expected_mcp':spec['tool'] in tools if spec['tool'] else not tools,**extra}
    if spec['id']=='order_status':checks['grounded_status']=result is not None and 'DEMO-1001' in result.message and 'قيد الشحن' in result.message
    if spec['id']=='memory_followup':checks['remembered_arabic']=result is not None and 'عرب' in result.message
    if spec['id']=='ambiguous':checks['clarification']=result is not None and bool(re.search('رقم|حدد|أي طلب|توضيح',result.message)) and not tools
    return public_result(result,guard,audit,checks)

async def memory_fixture(store,approvals,out):
    session_id='task08-memory'
    arguments={'content':PREFERENCE}
    grant=issue_approval(approvals,customer_id='demo_customer_001',session_id=session_id,
        tool='save_memory',arguments=arguments,confirmed=True)
    async with mcp_connection(store,customer_id='demo_customer_001',session_id=session_id,approval_db=approvals) as session:
        scoped=ScopedMCP(session,{'save_memory'},arguments)
        saved=await scoped.call_tool('save_memory',{**arguments,'approval_id':grant})
        assert not saved.isError and saved.structuredContent['ok']
    write(out/'memory_setup.json',{'explicit_user_consent':True,'mcp_tool':'save_memory',
        'success':True,'process_closed_before_followup':True,'synthetic_content_only':True})

def main():
    run_id=uuid4().hex[:12]
    out=ROOT/'reports/evidence'/('task08_20261105_'+run_id)
    out.mkdir()
    (ROOT/'.runtime/task08_latest.json').write_text(json.dumps({'evidence':str(out)}),encoding='utf-8')
    report={'run_id':run_id,'started_utc':datetime.now(timezone.utc).isoformat(),'user_approved_exact_ticket_and_memory':True,
        'total_cap_usd':'1','task_cap_usd':'.005','daily_cap_usd':'.10','scenarios':{},'attempts_not_retried':True,
        'official_sources':['https://developers.openai.com/api/docs/models/gpt-5.4-mini','https://developers.openai.com/api/docs/guides/token-counting','https://help.openai.com/en/articles/4936856-understanding-and-counting-tokens']}
    (out/'runner.py').write_bytes(Path(__file__).read_bytes())
    (out/'guard.py').write_bytes(Path(__file__).with_name('task08_guard.py').read_bytes())
    historical=json.loads((ROOT/'reports/evidence/live_acceptance_closeout.json').read_text(encoding='utf-8'))['source_sha256']
    historical={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in historical if not p.endswith('.sqlite3')}
    historical['dist/SmartSupport_AI_Final_20261105.zip']=hashlib.sha256((ROOT/'dist/SmartSupport_AI_Final_20261105.zip').read_bytes()).hexdigest()
    if (ROOT/'data/smartsupport.db').exists():historical['data/smartsupport.db']=hashlib.sha256((ROOT/'data/smartsupport.db').read_bytes()).hexdigest()
    budget=None
    try:
        base=AgentConfig.from_env()
        assert os.environ.get('OPENAI_API_KEY','').strip()
        assert base.pricing.model==MODEL and base.service_tier=='default'
        tls=ssl_context();assert tls.check_hostname and tls.verify_mode==2
        cfg=replace(base,model=MODEL,max_turns=2,max_output_tokens=96,max_input_tokens=2500,max_retries=0,audit_path=None)
        daily=DailyCostLedger(cfg.daily_cost_path)
        previous_unknown=rows(cfg.daily_cost_path,"SELECT * FROM api_cost_requests WHERE status='unknown'")
        with closing(sqlite3.connect(cfg.daily_cost_path)) as db:
            db.execute('BEGIN IMMEDIATE');db.rollback()
        envelope=generation_bound(cfg.pricing)*5
        before=daily.snapshot()
        assert envelope<=Decimal('1') and Decimal(before['committed_usd'])+envelope<=cfg.max_daily_cost_usd
        report.update(key_present=True,model=MODEL,ledger_before=before,generation_envelope_usd=str(envelope),tls_verified=True)
        budget=RunBudget(daily,DailyCostLedger(out/'run_cost.sqlite3'),daily_limit=cfg.max_daily_cost_usd)
        with TemporaryDirectory(prefix='smartsupport-task08-') as temporary:
            temp=Path(temporary);store,approvals=temp/'store.sqlite3',temp/'approvals.sqlite3'
            init_database(store);seed_demo_data(store)
            plugin=LivePlugin(cfg,budget,out)
            os.environ['RUN_LIVE_OPENAI']='1'
            code=pytest.main(['-p','no:cacheprovider','-q','-rs','--tb=short',
                '--basetemp='+str(temp/'pytest'),'--junitxml='+str(out/'live_pytest.xml'),
                str(ROOT/'tests/test_agent_live.py')],plugins=[plugin])
            os.environ['RUN_LIVE_OPENAI']='0'
            if hasattr(plugin,'guard'):
                result=public_result(plugin.result,plugin.guard,plugin.audit,{'existing_live_pytest':code==0,
                    'search_faq_success':any(e['event_type']=='mcp_result' and e['status']=='success' and e['tool_name']=='search_faq' for e in plugin.audit.events)})
                report['scenarios']['shipping']=result
            else:raise ValueError('Live test skipped or setup failed')
            write(out/'shipping_result.json',result)
            if not budget.halted and result['usage_complete']:
                specs=[{'id':'order_status','tool':'get_order_status','session':'task08-order','message':'ما حالة الطلب DEMO-1001؟'},
                    {'id':'create_ticket','tool':'create_ticket','session':'task08-ticket','message':'أنشئ تذكرة للطلب DEMO-1002 بالوصف الحرفي: '+DESCRIPTION},
                    {'id':'memory_followup','tool':'load_memory','session':'task08-memory','message':'ما اللغة التي فضلتها سابقًا؟ تحقق من الذاكرة المحفوظة.',
                     'history':[{'role':'user','content':'أفضل التواصل بالعربية. احفظ تفضيلي بعد موافقتي.'},{'role':'assistant','content':'تم حفظ تفضيل التواصل بالعربية بعد موافقتك.'}]},
                    {'id':'ambiguous','tool':None,'session':'task08-ambiguous','message':'أريد متابعة طلبي الآخر.'}]
                for spec in specs:
                    if budget.halted:break
                    if spec['id']=='memory_followup':asyncio.run(memory_fixture(store,approvals,out))
                    result=asyncio.run(scenario(spec,store,approvals,cfg,budget,out))
                    report['scenarios'][spec['id']]=result
                    write(out/(spec['id']+'_result.json'),result)
                    if not result['usage_complete']:budget.halted=True
            report['ledger_after']=daily.snapshot()
            report['previous_unknown_unchanged']=rows(cfg.daily_cost_path,"SELECT * FROM api_cost_requests WHERE status='unknown' AND request_id IN ("+','.join('?' for _ in previous_unknown)+")",tuple(row[0] for row in previous_unknown))==previous_unknown if previous_unknown else True
    except BaseException as exc:
        report['execution_error']={'type':type(exc).__name__,'phase':'runner'}
        print(json.dumps({'safe_error_type':type(exc).__name__}),flush=True)
    finally:
        os.environ['RUN_LIVE_OPENAI']='0'
        report['ended_utc']=datetime.now(timezone.utc).isoformat()
        report['historical_changed']=[p for p,digest in historical.items() if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=digest]
        report['package_unchanged']='dist/SmartSupport_AI_Final_20261105.zip' not in report['historical_changed']
        values=list(report['scenarios'].values())
        report['input_tokens']=sum(v['input_tokens'] for v in values)
        report['output_tokens']=sum(v['output_tokens'] for v in values)
        report['cached_tokens']=sum(v['cached_tokens'] for v in values)
        report['known_cost_usd']=str(sum((Decimal(v['known_cost_usd']) for v in values),Decimal(0)))
        report['total_cost_usd']=report['known_cost_usd'] if values and all(v['usage_complete'] for v in values) else None
        report['status']='PASS' if len(values)==5 and all(v['status']=='PASS' for v in values) and not report.get('execution_error') and not report['historical_changed'] else 'PARTIAL'
        write(out/'result.json',report)
        print(json.dumps({'evidence':str(out),'status':report['status'],'scenarios':{k:v['status'] for k,v in report['scenarios'].items()},
            'input_tokens':report['input_tokens'],'output_tokens':report['output_tokens'],'cost_usd':report['total_cost_usd']},ensure_ascii=True),flush=True)

if __name__=='__main__':main()
