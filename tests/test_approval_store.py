from concurrent.futures import ThreadPoolExecutor

import pytest

from approval_store import consume_approval, issue_approval
from security import WriteApprovalRequired


def test_issuer_needs_explicit_confirmation(tmp_path):
    path = tmp_path / "grants.sqlite3"
    with pytest.raises(WriteApprovalRequired):
        issue_approval(path, customer_id="c", session_id="s", tool="save_memory", arguments={"content": "x"})
    assert not path.exists()


@pytest.mark.parametrize("ttl", [0, -1, 301, True, "120"])
def test_issuer_limits_grant_lifetime(tmp_path, ttl):
    with pytest.raises(ValueError):
        issue_approval(tmp_path / "grants.sqlite3", customer_id="c", session_id="s",
                       tool="save_memory", arguments={"content": "x"}, confirmed=True, ttl_seconds=ttl)


def test_concurrent_consumption_allows_only_one_write_authorization(tmp_path):
    path = tmp_path / "grants.sqlite3"
    arguments = {"content": "exact"}
    approval_id = issue_approval(path, customer_id="c", session_id="s", tool="save_memory",
                                 arguments=arguments, confirmed=True)

    def consume():
        try:
            consume_approval(path, approval_id=approval_id, customer_id="c", session_id="s",
                             tool="save_memory", arguments=arguments)
            return True
        except WriteApprovalRequired:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: consume(), range(2))) == [False, True]
