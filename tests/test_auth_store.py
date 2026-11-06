import concurrent.futures
import hashlib
import secrets
import sqlite3
from contextlib import closing

import pytest

from auth_store import CUSTOMER_IDS, AuthError, AuthStore
from web_controls import WebError
from web_runtime import WebBackend

PASSWORD=secrets.token_urlsafe(24)


def test_password_is_salted_hashed_and_login_returns_server_principal(tmp_path):
    auth=AuthStore(tmp_path)
    auth.create_user("SyntheticCustomer",PASSWORD,customer_id="demo_customer_001")
    with closing(sqlite3.connect(auth.path)) as db:
        encoded=db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert encoded.startswith("pbkdf2_sha256$600000$")
    assert PASSWORD not in encoded and encoded.split("$")[2] and encoded.split("$")[3]
    token,principal=auth.login("syntheticcustomer",PASSWORD)
    assert principal.role=="customer" and principal.customer_id=="demo_customer_001"
    assert auth.authenticate(token)==principal
    with closing(sqlite3.connect(auth.path)) as db:
        token_hash=db.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert token_hash==hashlib.sha256(token.encode()).hexdigest() and token not in token_hash


def test_wrong_password_rate_limit_and_generic_error(tmp_path):
    auth=AuthStore(tmp_path)
    auth.create_user("alice",PASSWORD,customer_id="demo_customer_002")
    for _ in range(5):
        with pytest.raises(AuthError): auth.login("alice","incorrect-password")
    with pytest.raises(AuthError): auth.login("alice",PASSWORD)
    with pytest.raises(AuthError): auth.login("missing-user",PASSWORD)


def test_expiry_logout_and_operator_separation(tmp_path):
    now=[1000.0]
    auth=AuthStore(tmp_path,clock=lambda:now[0])
    auth.create_user("alice",PASSWORD,customer_id="demo_customer_003")
    auth.create_user("operator",PASSWORD,role="operator")
    token,customer=auth.login("alice",PASSWORD)
    with pytest.raises(PermissionError): auth.set_live_enabled(token,True)
    auth.logout(token)
    with pytest.raises(AuthError): auth.authenticate(token)
    op_token,operator=auth.login("operator",PASSWORD)
    assert operator.role=="operator" and operator.customer_id is None
    backend=WebBackend(tmp_path)
    with pytest.raises(WebError): backend.create_account_session(operator)
    backend.close()
    auth.set_live_enabled(op_token,True)
    assert auth.get_live_enabled(op_token)
    now[0]+=8*60*60+1
    with pytest.raises(AuthError): auth.authenticate(op_token)


def test_customer_identity_is_fixed_to_synthetic_allowlist(tmp_path):
    auth=AuthStore(tmp_path)
    with pytest.raises(ValueError): auth.create_user("real",PASSWORD,customer_id="real_customer_42")
    assert CUSTOMER_IDS=={f"demo_customer_{i:03d}" for i in range(1,6)}


def test_concurrent_wrong_password_attempts_are_counted_atomically(tmp_path):
    auth=AuthStore(tmp_path)
    auth.create_user("alice",PASSWORD,customer_id="demo_customer_004")
    def attempt(_):
        try: auth.login("alice","incorrect-password")
        except AuthError: return True
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(attempt,range(8)))
    with pytest.raises(AuthError): auth.login("alice",PASSWORD)


def test_authenticated_account_owns_customer_and_conversations_restore_by_account(tmp_path):
    auth=AuthStore(tmp_path)
    auth.create_user("alice",PASSWORD,customer_id="demo_customer_001")
    auth.create_user("bob",PASSWORD,customer_id="demo_customer_002")
    alice_token,alice=auth.login("alice",PASSWORD)
    _,bob=auth.login("bob",PASSWORD)
    backend=WebBackend(tmp_path)
    visitor=backend.create_account_session(alice)
    with pytest.raises(WebError):
        backend.new_conversation(visitor,"عميل تجريبي 2")
    conversation=backend.new_conversation(visitor,"عميل تجريبي 1")
    backend.close()

    restarted=WebBackend(tmp_path)
    restored=restarted.restore_account(auth.authenticate(alice_token))
    assert restored==visitor
    assert conversation in restarted.visitors[restored].conversation_ids
    assert restarted.visitors[restored].customer_id==alice.customer_id
    other_visitor=restarted.restore_account(bob)
    assert other_visitor != restored and not restarted.visitors[other_visitor].conversation_ids
    with pytest.raises(WebError):
        restarted.new_conversation(restored,"عميل تجريبي 2")
    restarted.logout_account(alice.user_id)
    with pytest.raises(WebError):
        restarted.view(restored,conversation)
    restarted.close()
