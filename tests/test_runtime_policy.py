from runtime_policy import direct_demo_allowed


def test_direct_demo_allows_loopback_with_live_disabled(monkeypatch):
    monkeypatch.delenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", raising=False)
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "0")
    assert direct_demo_allowed("127.0.0.1")
    assert not direct_demo_allowed("0.0.0.0")


def test_direct_demo_is_disabled_for_live_and_allowed_for_public_demo(monkeypatch):
    monkeypatch.delenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", raising=False)
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    assert not direct_demo_allowed("127.0.0.1")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "0")
    monkeypatch.setenv("SMARTSUPPORT_PUBLIC_DEMO_ONLY", "1")
    assert direct_demo_allowed("0.0.0.0")
    monkeypatch.setenv("SMARTSUPPORT_ENABLE_LOCAL_LIVE", "1")
    assert direct_demo_allowed("0.0.0.0")
