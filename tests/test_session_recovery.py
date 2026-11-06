"""Restart/ownership tests: isolated SQLite, no paid provider, no implicit replay."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from daily_cost import DailyCostLedger
from web_controls import WebError
from web_runtime import WebBackend
@pytest.fixture(autouse=True)
def no_paid_provider(monkeypatch):
    monkeypatch.setattr('web_runtime.AsyncOpenAI',lambda **kw: pytest.fail('Paid API forbidden'))


class Clock:
    def __init__(self): self.now=10000.0
    def __call__(self): return self.now
    def advance(self,n): self.now+=n


class Worker:
    def __init__(self,*args):
        self.calls=[]; self.closed=False; self.processing=Event(); self.error=None
        self.thread=SimpleNamespace(is_alive=lambda: not self.closed)
        self.model='fake-test-model'
    def close(self): self.closed=True
    def call(self,kind,payload):
        self.calls.append((kind,payload))
        result={'task_id':payload.get('request_id',payload.get('task_id')),
                'message':'إجابة محفوظة','stop_reason':'final_answer','pending_approval':None,
                'input_tokens':15,'output_tokens':4,'estimated_cost_usd':'0.0001',
                'usage_complete':True,'tools_used':[],'model':self.model,
                'cost_summary':{'usage_source':'simulation'}}
        if payload.get('message')=='pending':
            result.update(stop_reason='pending_approval',pending_approval={
                'pending_id':'synthetic-pending','tool':'create_ticket',
                'arguments':{'order_id':'DEMO-1002','description':'اختبار معزول'}})
        return result


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr('web_runtime.AgentWorker',Worker)


def start(root,clock=None,**kwargs):
    return WebBackend(root,clock=clock,wall_clock=clock,**kwargs)


def identity(backend,label='عميل تجريبي 1'):
    visitor,secret=backend.create_browser_session()
    cid=backend.new_conversation(visitor,label)
    return visitor,secret,cid


def restart(backend,root,clock=None):
    backend.close()
    return start(root,clock)


def test_completed_context_restores_only_with_independent_secret_and_never_replays(tmp_path,worker,monkeypatch):
    backend=start(tmp_path)
    visitor,secret,cid=identity(backend)
    original=backend.send(visitor,cid,'مرحبا',request_id='browser-request-1')
    original_messages=backend.view(visitor,cid)['messages']
    backend=restart(backend,tmp_path)
    try:
        monkeypatch.setattr('web_runtime.AgentWorker',lambda *a: pytest.fail('Recovery must not create a worker'))
        with pytest.raises(WebError,match='session_lost'): backend.view(visitor,cid)
        with pytest.raises(WebError,match='session_lost'): backend.restore_visitor(visitor,'0'*64)
        assert backend.restore_visitor(visitor,secret)[0]['id']==cid
        assert backend.view(visitor,cid)['messages']==original_messages
        assert backend.view(visitor,cid)['result']==original
        assert backend.view(visitor,cid)['result']['task_status']=='completed'
        assert backend.send(visitor,cid,'مرحبا',request_id='browser-request-1')==original
        assert backend.view(visitor,cid)['messages']==original_messages
        with pytest.raises(WebError,match='invalid_input'):
            backend.send(visitor,cid,'محتوى مختلف',request_id='browser-request-1')
        assert backend.conversations[cid].worker is None
    finally: backend.close()


def test_wrong_owner_mcp_session_and_conversation_ids_cannot_unlock_other_customer(tmp_path,worker):
    backend=start(tmp_path)
    a,sa,ca=identity(backend)
    b,sb,cb=identity(backend,'عميل تجريبي 2')
    backend.send(a,ca,'سياق ألف')
    backend.send(b,cb,'سياق باء')
    mcp_session=backend.conversations[ca].context.session_id
    backend=restart(backend,tmp_path)
    try:
        for wrong_id,wrong_secret in ((a,sb),(b,sa),(ca,sa),(mcp_session,sa),(a,'')):
            with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(wrong_id,wrong_secret)
        backend.restore_visitor(a,sa)
        assert b not in backend.visitors and cb not in backend.conversations
        with pytest.raises(WebError,match='session_lost'): backend.view(a,cb)
        assert 'سياق باء' not in str(backend.view(a,ca))
        backend.restore_visitor(b,sb)
        with pytest.raises(WebError,match='session_lost'):backend.view(b,ca)
        assert 'سياق ألف' not in str(backend.view(b,cb))
    finally:backend.close()


def test_expired_identity_is_not_renewed_by_restart_even_with_correct_secret(tmp_path,worker):
    clock=Clock(); backend=start(tmp_path,clock)
    v,s,cid=identity(backend)
    backend.close();clock.advance(1800)
    backend=start(tmp_path,clock)
    try:
        with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
        fresh,_=backend.create_browser_session()
        with pytest.raises(WebError,match='session_lost'):backend.view(fresh,cid)
        assert (tmp_path/cid/'store.sqlite3').exists()
    finally:backend.close()


def test_cleanup_revocation_survives_restart_and_clock_rollback(tmp_path,worker):
    clock=Clock();backend=start(tmp_path,clock,visitor_idle_seconds=60)
    v,s,cid=identity(backend)
    clock.advance(60);assert backend.cleanup_expired_visitors()==1
    backend.close();clock.advance(-60)
    backend=start(tmp_path,clock)
    try:
        with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    finally:backend.close()


def test_shorter_new_ttl_and_wall_clock_rollback_fail_closed(tmp_path,worker):
    clock=Clock();backend=start(tmp_path,clock)
    v,s,_=identity(backend);backend.close();clock.advance(61)
    backend=start(tmp_path,clock,visitor_idle_seconds=60)
    with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    backend.close();clock.advance(-100)
    backend=start(tmp_path,clock)
    try:
        with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    finally:backend.close()


def test_pending_after_restart_is_cancelled_without_grant_or_execution(tmp_path,worker,monkeypatch):
    backend=start(tmp_path);v,s,cid=identity(backend)
    pending=backend.send(v,cid,'pending',request_id='pending-request')
    backend=restart(backend,tmp_path)
    try:
        monkeypatch.setattr('web_runtime.issue_approval',lambda *a,**kw: pytest.fail('No recovered grant'))
        backend.restore_visitor(v,s)
        result=backend.view(v,cid)['result']
        assert result['task_status']=='cancelled' and result['requires_new_action']
        assert result['stop_reason']=='approval_restart_required' and result['pending_approval'] is None
        with pytest.raises(WebError,match='already_handled'):
            backend.decide(v,cid,pending['pending_approval']['pending_id'],'approve')
        assert backend.send(v,cid,'pending',request_id='pending-request')==result
        assert not (tmp_path/cid/'approvals.sqlite3').exists()
        assert backend.conversations[cid].worker is None
    finally:backend.close()


@pytest.mark.parametrize('changed', [False,True])
def test_expired_or_changed_pending_details_cannot_issue_approval(tmp_path,worker,monkeypatch,changed):
    clock=Clock();backend=start(tmp_path,clock)
    v,s,cid=identity(backend);pending=backend.send(v,cid,'pending')
    old_worker=backend.conversations[cid].worker
    if changed:backend.conversations[cid].result['pending_approval']['arguments']['description']='تفاصيل أخرى'
    else:clock.advance(120)
    monkeypatch.setattr('web_runtime.issue_approval',lambda *a,**kw: pytest.fail('Invalid approval'))
    try:
        with pytest.raises(WebError,match='approval_invalid'):
            backend.decide(v,cid,pending['pending_approval']['pending_id'],'approve')
        assert backend.view(v,cid)['result']['task_status']=='cancelled'
        assert len(old_worker.calls)==1 and old_worker.closed
        assert backend.conversations[cid].worker is None
    finally:backend.close()


def test_real_mcp_ticket_replays_same_operation_after_restart_and_new_request(tmp_path):
    backend=start(tmp_path);v,s,cid=identity(backend)
    prompt='إنشاء شكوى للطلب DEMO-1002'
    pending=backend.send(v,cid,prompt,request_id='ticket-request')
    done=backend.decide(v,cid,pending['pending_approval']['pending_id'],'approve')
    backend=restart(backend,tmp_path)
    try:
        backend.restore_visitor(v,s)
        assert backend.send(v,cid,prompt,request_id='ticket-request')==done
        again=backend.send(v,cid,prompt,request_id='new-request-same-write')
        replay=backend.decide(v,cid,again['pending_approval']['pending_id'],'approve')
        assert replay['stop_reason']=='final_answer'
        with closing(sqlite3.connect(tmp_path/cid/'store.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==4
            assert db.execute('SELECT count(*) FROM write_operations').fetchone()[0]==1
        with closing(sqlite3.connect(tmp_path/cid/'approvals.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM write_approvals').fetchone()[0]==2
    finally:backend.close()


def test_failed_and_cancelled_tasks_are_restored_as_terminal(tmp_path,worker):
    backend=start(tmp_path);v,s,cid=identity(backend)
    first=backend.send(v,cid,'pending',request_id='cancelled-request')
    cancelled=backend.decide(v,cid,first['pending_approval']['pending_id'],'reject')
    assert cancelled['task_status']=='cancelled' and cancelled['stop_reason']=='cancelled'
    def fail(*a):raise WebError('unknown_result')
    backend.conversations[cid].worker.call=fail
    with pytest.raises(WebError):backend.send(v,cid,'فشل اصطناعي',request_id='failed-request')
    backend=restart(backend,tmp_path)
    try:
        backend.restore_visitor(v,s)
        assert backend.view(v,cid)['result']['task_status']=='failed'
        assert backend.send(v,cid,'pending',request_id='cancelled-request')==cancelled
        failed=backend.send(v,cid,'فشل اصطناعي',request_id='failed-request')
        assert failed['task_status']=='failed' and failed['outcome_unknown']
        assert backend.conversations[cid].worker is None
    finally:backend.close()


def test_daily_cost_reservations_actuals_and_session_limits_persist(tmp_path,worker):
    ledger=DailyCostLedger(tmp_path/'cost.sqlite3')
    assert ledger.reserve('known-api-request',Decimal('.001'),Decimal('.10'))
    ledger.settle('known-api-request',Decimal('.0008'))
    assert ledger.reserve('unknown-api-request',Decimal('.002'),Decimal('.10'))
    before=ledger.snapshot()
    backend=start(tmp_path);v,s,cid=identity(backend)
    result=backend.send(v,cid,'تحقق من الحالة')
    backend=restart(backend,tmp_path)
    try:
        backend.restore_visitor(v,s)
        assert backend.view(v,cid)['result']['estimated_cost_usd']==result['estimated_cost_usd']
        assert DailyCostLedger(tmp_path/'cost.sqlite3').snapshot()==before
        assert before['committed_usd']=='0.0028' and before['unresolved_requests']==1
        with closing(sqlite3.connect(tmp_path/'web_controls.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM web_requests').fetchone()[0]==2
    finally:backend.close()


def test_parallel_recovery_and_request_retry_do_not_repeat_worker(tmp_path,worker):
    backend=start(tmp_path);v,s,cid=identity(backend);backend=restart(backend,tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _:backend.restore_visitor(v,s),range(16)))
            results=list(pool.map(lambda _:backend.send(v,cid,'نفس الطلب',request_id='one-request'),range(8)))
        assert all(r==results[0] for r in results)
        assert len(backend.conversations[cid].worker.calls)==1
        assert len(backend.visitors)==1 and backend.visitors[v].operations==0
    finally:backend.close()


def test_live_backend_exclusive_lock_prevents_takeover_and_close_during_execution(tmp_path,worker):
    backend=start(tmp_path);v,s,cid=identity(backend)
    with pytest.raises(WebError,match='busy'):start(tmp_path)
    entered,release=Event(),Event()
    original=Worker.call
    def block(self,*args):
        entered.set();assert release.wait(5)
        return original(self,*args)
    Worker.call=block
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            job=pool.submit(backend.send,v,cid,'اختبار التزامن')
            assert entered.wait(2)
            with pytest.raises(WebError,match='busy'):backend.close()
            with pytest.raises(WebError,match='busy'):start(tmp_path)
            release.set();assert job.result(timeout=3)['task_status']=='completed'
    finally:
        Worker.call=original
        backend.close()


def test_process_crash_mid_task_recovers_unknown_without_model_or_mcp(tmp_path,worker,monkeypatch):
    root=Path(__file__).resolve().parents[1]
    script=r'''
import json,os,sys
from pathlib import Path
import web_runtime
class CrashWorker:
    def __init__(self,*a):pass
    def call(self,*a):os._exit(23)
web_runtime.AgentWorker=CrashWorker
b=web_runtime.WebBackend(sys.argv[1])
v,s=b.create_browser_session()
c=b.new_conversation(v,'عميل تجريبي 1')
Path(sys.argv[1],'test_credentials.json').write_text(json.dumps([v,s,c]),encoding='utf-8')
b.send(v,c,'مهمة انقطعت',request_id='crashed-request')
'''
    process=subprocess.run([sys.executable,'-c',script,str(tmp_path)],cwd=root,capture_output=True,timeout=15)
    assert process.returncode==23
    v,s,cid=json.loads((tmp_path/'test_credentials.json').read_text(encoding='utf-8'))
    backend=start(tmp_path)
    try:
        monkeypatch.setattr('web_runtime.AgentWorker',lambda *a: pytest.fail('Crash recovery replayed work'))
        backend.restore_visitor(v,s)
        result=backend.view(v,cid)['result']
        assert result['task_status']=='failed' and result['stop_reason']=='interrupted'
        assert result['outcome_unknown'] and not result['usage_complete']
        assert backend.send(v,cid,'مهمة انقطعت',request_id='crashed-request')==result
    finally:backend.close()


def test_secret_is_hashed_and_archive_ownership_survives_restart(tmp_path,worker):
    backend=start(tmp_path);v,s,old=identity(backend)
    backend.send(v,old,'السجل القديم')
    current=backend.new_conversation(v,'عميل تجريبي 2',previous=old)
    assert s.encode() not in (tmp_path/'web_sessions.sqlite3').read_bytes()
    backend=restart(backend,tmp_path)
    try:
        backend.restore_visitor(v,s)
        assert backend.view(v,current)['customer_label']=='عميل تجريبي 2'
        archives=backend.history(v,'عميل تجريبي 1','Demo')
        assert archives[0]['id']==old and 'السجل القديم' in str(archives)
        with pytest.raises(WebError,match='session_lost'):backend.view(v,old)
    finally:backend.close()


def test_unrestored_registrations_still_enforce_visitor_capacity(tmp_path,worker):
    clock=Clock();backend=start(tmp_path,clock)
    original=[backend.create_browser_session() for _ in range(64)]
    backend=restart(backend,tmp_path,clock)
    try:
        with pytest.raises(WebError,match='busy'):backend.new_visitor()
        clock.advance(1800)
        assert backend.new_visitor() not in {v for v,s in original}
        for v,s in original:
            with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    finally:backend.close()


def test_concurrent_decisions_have_one_durable_winner(tmp_path,worker):
    backend=start(tmp_path);v,s,cid=identity(backend)
    pending=backend.send(v,cid,'pending')
    def decide(_):
        try:return backend.decide(v,cid,pending['pending_approval']['pending_id'],'approve')
        except WebError as error:
            assert error.code=='already_handled'
            return None
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(decide,range(6)))
        assert len([r for r in results if r])==1
        assert [kind for kind,payload in backend.conversations[cid].worker.calls]==['send','decision']
        with closing(sqlite3.connect(tmp_path/'web_sessions.sqlite3')) as db:
            # Legacy table is retained for upgrade compatibility, but is no longer
            # a write authority; SQLite approval/store ledgers provide idempotency.
            assert db.execute('SELECT count(*) FROM write_attempts').fetchone()[0]==0
    finally:backend.close()


def test_changed_worker_pending_is_rejected_before_minting_grant(tmp_path,monkeypatch):
    backend=start(tmp_path);v,s,cid=identity(backend)
    pending=backend.send(v,cid,'إنشاء شكوى للطلب DEMO-1002')
    backend.conversations[cid].worker.pending['arguments']['description']='تفاصيل غير موافق عليها'
    monkeypatch.setattr('web_runtime.issue_approval',lambda *a,**kw: pytest.fail('Changed worker details granted'))
    try:
        with pytest.raises(WebError,match='unknown_result'):
            backend.decide(v,cid,pending['pending_approval']['pending_id'],'approve')
        assert not (tmp_path/cid/'approvals.sqlite3').exists()
        with closing(sqlite3.connect(tmp_path/cid/'store.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==3
    finally:backend.close()


def test_crash_after_ticket_commit_before_ui_journal_never_replays_write(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=r'''
import json,os,sys
from pathlib import Path
from web_runtime import WebBackend
b=WebBackend(sys.argv[1])
v,s=b.create_browser_session()
c=b.new_conversation(v,'عميل تجريبي 1')
Path(sys.argv[1],'test_credentials.json').write_text(json.dumps([v,s,c]),encoding='utf-8')
p=b.send(v,c,'إنشاء شكوى للطلب DEMO-1002',request_id='write-before-crash')
def crash(*args,**kwargs):
    b.conversations[c].worker.close()
    os._exit(24)
b._store=crash
b.decide(v,c,p['pending_approval']['pending_id'],'approve')
'''
    process=subprocess.run([sys.executable,'-c',script,str(tmp_path)],cwd=root,capture_output=True,timeout=30)
    assert process.returncode==24
    v,s,cid=json.loads((tmp_path/'test_credentials.json').read_text(encoding='utf-8'))
    backend=start(tmp_path)
    try:
        backend.restore_visitor(v,s)
        interrupted=backend.view(v,cid)['result']
        assert interrupted['task_status']=='failed' and interrupted['outcome_unknown']
        assert interrupted['stop_reason']=='interrupted'
        with closing(sqlite3.connect(tmp_path/cid/'store.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==4
        retry=backend.send(v,cid,'إنشاء شكوى للطلب DEMO-1002',request_id='deliberate-new-request')
        replay=backend.decide(v,cid,retry['pending_approval']['pending_id'],'approve')
        assert replay['stop_reason']=='final_answer'
        with closing(sqlite3.connect(tmp_path/cid/'store.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==4
            assert db.execute('SELECT count(*) FROM write_operations').fetchone()[0]==1
    finally:backend.close()


def test_observed_expiry_cannot_be_reversed_by_later_restart_or_clock_rollback(tmp_path,worker):
    clock=Clock();backend=start(tmp_path,clock)
    v,s,cid=identity(backend);backend.close()
    clock.advance(1801);backend=start(tmp_path,clock)
    with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    backend.close();clock.advance(-1000)
    backend=start(tmp_path,clock)
    try:
        with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    finally:backend.close()


def test_retry_pending_request_returns_snapshot_without_consuming_quota_or_calling_worker(tmp_path,worker):
    backend=start(tmp_path);v,s,cid=identity(backend)
    try:
        pending=backend.send(v,cid,'pending',request_id='same-pending-request')
        assert backend.send(v,cid,'pending',request_id='same-pending-request')==pending
        assert len(backend.conversations[cid].worker.calls)==1
        with closing(sqlite3.connect(tmp_path/'web_controls.sqlite3')) as db:
            assert db.execute('SELECT count(*) FROM web_requests').fetchone()[0]==2
    finally:backend.close()


def test_streamlit_resource_restart_restores_context_without_running_worker(tmp_path,worker,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from web_ui import get_web_backend
    monkeypatch.setenv('SMARTSUPPORT_WEB_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('SMARTSUPPORT_PUBLIC_DEMO_ONLY','1')
    get_web_backend.clear()
    try:
        ui=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=30).run()
        ui.chat_input[0].set_value('سياق خاص للاختبار').run()
        assert not ui.exception
        visitor,cid=ui.session_state['visitor'],ui.session_state['conversation']
        messages=list(ui.session_state['messages'])
        get_web_backend.clear()
        monkeypatch.setattr('web_runtime.AgentWorker',lambda *a: pytest.fail('UI restart replayed a task'))
        ui.run()
        assert not ui.exception
        assert ui.session_state['visitor']==visitor and ui.session_state['conversation']==cid
        assert ui.session_state['messages']==messages
    finally:get_web_backend.clear()


def test_new_direct_demo_session_cannot_restore_another_browser_context(tmp_path,worker,monkeypatch):
    from streamlit.testing.v1 import AppTest
    from web_ui import get_web_backend
    root=Path(__file__).resolve().parents[1]
    monkeypatch.setenv('SMARTSUPPORT_WEB_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('SMARTSUPPORT_PUBLIC_DEMO_ONLY','1')
    get_web_backend.clear()
    try:
        first=AppTest.from_file(str(root/'app.py'),default_timeout=30).run()
        first.chat_input[0].set_value('رسالة خاصة بجلسة ألف').run()
        visitor,cid=(first.session_state[k] for k in ('visitor','conversation'))
        get_web_backend.clear()
        second=AppTest.from_file(str(root/'app.py'),default_timeout=30).run()
        assert not second.exception
        assert second.session_state['visitor'] != visitor
        assert second.session_state['conversation'] != cid
        assert 'رسالة خاصة بجلسة ألف' not in str(second.session_state['messages'])
    finally:get_web_backend.clear()


def test_cleanup_expires_unrestored_registrations_without_removing_data(tmp_path,worker):
    clock=Clock();backend=start(tmp_path,clock)
    v,s,cid=identity(backend);backend=restart(backend,tmp_path,clock)
    try:
        clock.advance(1800)
        assert backend.cleanup_expired_visitors()==1
        assert backend.cleanup_expired_visitors()==0
        assert (tmp_path/cid/'store.sqlite3').exists()
        with pytest.raises(WebError,match='session_lost'):backend.restore_visitor(v,s)
    finally:backend.close()


def test_recovery_attempt_does_not_revoke_an_active_request_after_its_idle_ttl(tmp_path,worker,monkeypatch):
    clock=Clock();backend=start(tmp_path,clock,visitor_idle_seconds=60)
    v,s,cid=identity(backend)
    entered,release=Event(),Event();original=Worker.call
    def block(self,*args):
        entered.set();assert release.wait(5);return original(self,*args)
    monkeypatch.setattr(Worker,'call',block)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            job=pool.submit(backend.send,v,cid,'طلب نشط')
            assert entered.wait(2)
            clock.advance(120)
            assert backend.cleanup_expired_visitors()==0
            assert backend.restore_visitor(v,s)[0]['id']==cid
            release.set();assert job.result(timeout=3)['task_status']=='completed'
    finally:backend.close()
