"""Visitor expiry is tested with a monotonic clock, never real-time sleeps/API."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from web_controls import WebControls, WebError
from web_runtime import WebBackend


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_65th_visitor_is_admitted_after_default_idle_expiry(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr('web_runtime.monotonic', clock)
    backend = WebBackend(tmp_path)
    visitors = [backend.new_visitor() for _ in range(64)]
    with pytest.raises(WebError, match='busy'):
        backend.new_visitor()
    clock.advance(1801)
    fresh = backend.new_visitor()
    assert fresh not in visitors
    assert len(backend.visitors) == 1
    with pytest.raises(WebError, match='session_lost'):
        backend.new_conversation(visitors[0], 'عميل تجريبي 1')
    backend.close()


def service(tmp_path, clock, **kwargs):
    return WebBackend(tmp_path, clock=clock, visitor_idle_seconds=60, **kwargs)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'invalid'])
def test_invalid_timeout_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv('SMARTSUPPORT_VISITOR_IDLE_SECONDS', value)
    with pytest.raises(ValueError):
        WebBackend(tmp_path)


def test_configured_timeout_and_direct_override(tmp_path, monkeypatch):
    monkeypatch.setenv('SMARTSUPPORT_VISITOR_IDLE_SECONDS', '75')
    assert WebBackend(tmp_path/'env').visitor_idle_seconds == 75
    assert WebBackend(tmp_path/'override', visitor_idle_seconds=90).visitor_idle_seconds == 90
    for value in (True, 0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            WebBackend(tmp_path/'invalid', visitor_idle_seconds=value)


def test_reads_refresh_idle_and_expiry_boundary_is_exclusive(tmp_path):
    clock=Clock(); backend=service(tmp_path,clock)
    visitor=backend.new_visitor(); cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    clock.advance(59)
    assert backend.view(visitor,cid)['messages']==[]
    clock.advance(59)
    assert backend.cleanup_expired_visitors()==0
    assert backend.history(visitor,'عميل تجريبي 1','Demo')==[]
    clock.advance(60)
    with pytest.raises(WebError,match='session_lost'):
        backend.view(visitor,cid)
    assert cid in backend.conversations and visitor not in backend.visitors
    backend.close()


def test_expired_identity_cannot_read_send_approve_or_recover_data(tmp_path, monkeypatch):
    monkeypatch.setattr('web_runtime.AsyncOpenAI',lambda **kw: pytest.fail('Paid API forbidden'))
    clock=Clock(); backend=service(tmp_path,clock)
    visitor=backend.new_visitor();cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    pending=backend.send(visitor,cid,'إنشاء شكوى للطلب DEMO-1002')
    con=backend.conversations[cid]
    # Closing the idle worker makes the snapshot deterministic; preserves pending metadata.
    con.worker.close()
    files={p:p.read_bytes() for p in con.directory.iterdir() if p.is_file()}
    messages=list(con.messages); result=con.result.copy()
    clock.advance(60)
    assert backend.cleanup_expired_visitors()==1
    operations=[lambda:backend.view(visitor,cid),
                lambda:backend.send(visitor,cid,'مرحبا'),
                lambda:backend.decide(visitor,cid,pending['pending_approval']['pending_id'],'approve'),
                lambda:backend.history(visitor,'عميل تجريبي 1','Demo'),
                lambda:backend.new_conversation(visitor,'عميل تجريبي 1',previous=cid)]
    for operation in operations:
        with pytest.raises(WebError,match='session_lost'):operation()
    fresh=backend.new_visitor()
    for token in (visitor,fresh):
        with pytest.raises(WebError,match='session_lost'):backend.view(token,cid)
    assert backend.conversations[cid] is con
    assert con.messages==messages and con.result==result
    assert all(p.read_bytes()==data for p,data in files.items())
    with closing(sqlite3.connect(con.directory/'store.sqlite3')) as db:
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0]==3
    assert not(con.directory/'approvals.sqlite3').exists()
    backend.close()


def test_new_identity_cannot_reuse_retained_conversation_token(tmp_path,monkeypatch):
    clock=Clock();backend=service(tmp_path,clock)
    old=backend.new_visitor();cid=backend.new_conversation(old,'عميل تجريبي 1')
    clock.advance(61);backend.cleanup_expired_visitors()
    with monkeypatch.context() as patch:
        patch.setattr('web_runtime.secrets.token_hex',lambda size:old)
        with pytest.raises(WebError,match='busy'):backend.new_visitor()
    assert old not in backend.visitors and cid in backend.conversations
    fresh=backend.new_visitor()
    assert fresh!=old
    with pytest.raises(WebError,match='session_lost'):backend.view(fresh,cid)
    backend.close()


def test_concurrent_admission_never_exceeds_64_and_reclaims_expired_slots(tmp_path):
    clock=Clock();backend=service(tmp_path,clock)
    def attempt(_):
        try:return backend.new_visitor()
        except WebError as exc:
            assert exc.code=='busy'
            return None
    with ThreadPoolExecutor(max_workers=12) as workers:
        first=list(workers.map(attempt,range(96)))
    assert len([v for v in first if v])==64 and len(backend.visitors)==64
    clock.advance(61)
    with ThreadPoolExecutor(max_workers=12) as workers:
        second=list(workers.map(attempt,range(96)))
    accepted=[v for v in second if v]
    assert len(accepted)==64 and len(set(accepted))==64 and len(backend.visitors)==64
    assert not(set(accepted)&set(filter(None,first)))
    assert all(state.operations==0 for state in backend.visitors.values())
    backend.close()


class BlockingWorker:
    """Deterministic active-I/O probe; no model, transport, or business writes."""
    def __init__(self):
        self.entered,self.release,self.processing=Event(),Event(),Event()
        self.closed=False;self.error=None;self.model='fake-test-model'
        self.thread=SimpleNamespace(is_alive=lambda:not self.closed)

    def call(self,kind,payload):
        self.processing.set();self.entered.set()
        try:
            assert self.release.wait(5)
            return {'message':'تم اختبار الطلب','pending_approval':None,'stop_reason':'final_answer'}
        finally:self.processing.clear()

    def close(self):
        assert not self.processing.is_set()
        self.closed=True


def test_in_flight_request_is_not_expired_even_after_ttl_and_completion_refreshes(tmp_path):
    clock=Clock();backend=service(tmp_path,clock)
    visitor=backend.new_visitor();cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    idle=backend.new_visitor();worker=BlockingWorker();backend.conversations[cid].worker=worker
    with ThreadPoolExecutor(max_workers=2) as pool:
        request=pool.submit(backend.send,visitor,cid,'مرحبا')
        assert worker.entered.wait(2)
        clock.advance(120)
        cleanup=pool.submit(backend.cleanup_expired_visitors)
        assert cleanup.result(timeout=2)==1
        assert visitor in backend.visitors and idle not in backend.visitors and not worker.closed
        worker.release.set();assert request.result(timeout=2)['stop_reason']=='final_answer'
    clock.advance(59)
    assert backend.cleanup_expired_visitors()==0
    clock.advance(1)
    assert backend.cleanup_expired_visitors()==1 and worker.closed
    backend.close()


def test_queued_request_waiting_on_conversation_lock_is_pinned(tmp_path):
    clock=Clock();backend=service(tmp_path,clock)
    visitor=backend.new_visitor();cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    worker=BlockingWorker();backend.conversations[cid].worker=worker
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(backend.send,visitor,cid,'مرحبا')
        assert worker.entered.wait(2)
        # Holding conversation lock does not block admission of another operation lease.
        original=backend._get
        seen=Event()
        def observed(*args,**kwargs):
            seen.set();return original(*args,**kwargs)
        backend._get=observed
        queued=pool.submit(backend.view,visitor,cid)
        assert seen.wait(2)
        assert backend.visitors[visitor].operations==2
        clock.advance(120)
        assert pool.submit(backend.cleanup_expired_visitors).result(timeout=2)==0
        worker.release.set()
        first.result(timeout=2);assert queued.result(timeout=2)['messages']
    assert backend.visitors[visitor].operations==0
    backend.close()


def test_timeout_left_background_worker_processing_is_not_closed(tmp_path):
    clock=Clock();backend=service(tmp_path,clock)
    visitor=backend.new_visitor();cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    worker=BlockingWorker();backend.conversations[cid].worker=worker
    worker.processing.set();clock.advance(61)
    assert backend.cleanup_expired_visitors()==0 and not worker.closed
    worker.processing.clear()
    assert backend.cleanup_expired_visitors()==1 and worker.closed
    backend.close()


def test_expired_worker_closes_outside_registry_lock(tmp_path):
    clock=Clock();backend=service(tmp_path,clock)
    old=backend.new_visitor();old_cid=backend.new_conversation(old,'عميل تجريبي 1')
    clock.advance(59)
    live=backend.new_visitor();live_cid=backend.new_conversation(live,'عميل تجريبي 2')
    finished=Event();observations=[]
    class Closing:
        processing=Event()
        def close(self):
            def read():
                backend.view(live,live_cid);finished.set()
            Thread(target=read,daemon=True).start()
            observations.append(finished.wait(2))
    backend.conversations[old_cid].worker=Closing()
    clock.advance(1)
    assert backend.cleanup_expired_visitors()==1
    assert observations==[True] and live in backend.visitors
    backend.conversations[old_cid].worker=None
    backend.close()


def populate_registry(backend,visitor,cid,count):
    template=backend.conversations[cid]
    for i in range(count-1):
        key=f'retained-{i}'
        backend.conversations[key]=replace(template,active=False,worker=None)
        backend.visitors[visitor].conversation_ids.add(key)


def test_retained_conversations_do_not_block_new_registration_after_expiry(tmp_path):
    clock=Clock();backend=service(tmp_path,clock)
    old=backend.new_visitor();cid=backend.new_conversation(old,'عميل تجريبي 1')
    populate_registry(backend,old,cid,128)
    with pytest.raises(WebError,match='busy'):backend.new_conversation(old,'عميل تجريبي 1')
    clock.advance(61);fresh=backend.new_visitor()
    new=backend.new_conversation(fresh,'عميل تجريبي 2')
    assert len(backend.conversations)==129 and cid in backend.conversations
    assert backend.view(fresh,new)['customer_label']=='عميل تجريبي 2'
    with pytest.raises(WebError,match='session_lost'):backend.view(fresh,cid)
    backend.close()


def test_concurrent_conversation_creation_reserves_capacity_before_io(tmp_path,monkeypatch):
    clock=Clock();backend=service(tmp_path,clock)
    visitor=backend.new_visitor();cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    populate_registry(backend,visitor,cid,127)
    entered,release=Event(),Event()
    import web_runtime
    original=web_runtime.init_database
    def blocked(path):
        entered.set();assert release.wait(4);return original(path)
    monkeypatch.setattr(web_runtime,'init_database',blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(backend.new_conversation,visitor,'عميل تجريبي 1')
        assert entered.wait(2)
        with pytest.raises(WebError,match='busy'):backend.new_conversation(visitor,'عميل تجريبي 1')
        assert backend._pending_conversations==1
        release.set();assert first.result(timeout=3) in backend.conversations
    assert backend._pending_conversations==0 and len(backend.conversations)==128
    backend.close()


def test_failed_operation_and_initialization_release_leases_and_reservations(tmp_path,monkeypatch):
    clock=Clock();backend=service(tmp_path,clock)
    visitor=backend.new_visitor()
    original_cid=backend.new_conversation(visitor,'عميل تجريبي 1')
    def broken(path):raise OSError('test initialization failure')
    monkeypatch.setattr('web_runtime.init_database',broken)
    with pytest.raises(OSError):backend.new_conversation(visitor,'عميل تجريبي 1',previous=original_cid)
    assert backend._pending_conversations==0 and backend.visitors[visitor].operations==0
    assert backend.view(visitor,original_cid)['messages']==[]
    clock.advance(61)
    assert backend.cleanup_expired_visitors()==1
    backend.close()


def test_cleanup_does_not_reset_persistent_limits_or_decision_claims(tmp_path):
    clock=Clock();controls=WebControls(tmp_path/'controls.sqlite3',global_per_hour=1)
    backend=service(tmp_path,clock,controls=controls)
    old=backend.new_visitor();cid=backend.new_conversation(old,'عميل تجريبي 1')
    controls.claim_decision(cid,'synthetic-pending')
    clock.advance(61)
    assert backend.cleanup_expired_visitors()==1
    fresh=backend.new_visitor()
    with pytest.raises(WebError,match='rate_limit'):
        backend.new_conversation(fresh,'عميل تجريبي 1')
    with pytest.raises(WebError,match='already_handled'):
        controls.claim_decision(cid,'synthetic-pending')
    assert backend._pending_conversations==0
    with closing(sqlite3.connect(controls.path)) as db:
        assert db.execute('SELECT count(*) FROM web_requests').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM web_decisions').fetchone()[0]==1
    backend.close()
