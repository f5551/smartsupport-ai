"""TLS policy and actual SDK transport inheritance; all tests are offline."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import ssl

import pytest

import openai_transport as transport


def assert_verified(context):
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_shared_context_requires_chain_and_hostname_verification():
    context = transport.ssl_context()
    assert_verified(context)
    assert context is transport.ssl_context()


def test_concurrent_workers_share_one_context():
    transport._shared_ssl_context.cache_clear()
    with ThreadPoolExecutor(max_workers=8) as workers:
        contexts = list(workers.map(lambda _: transport.ssl_context(), range(8)))
    assert len({id(context) for context in contexts}) == 1
    assert_verified(contexts[0])


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_trust_source_is_platform_specific(monkeypatch, platform):
    calls = []
    original = ssl.create_default_context
    def create(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(transport.sys, "platform", platform)
    monkeypatch.setattr(transport.ssl, "create_default_context", create)
    # Uncached invocation avoids changing the process-wide cached context.
    context = transport._shared_ssl_context.__wrapped__()
    assert_verified(context)
    assert calls[0]["purpose"] == ssl.Purpose.SERVER_AUTH
    if platform == "win32":
        assert "cafile" not in calls[0]  # Native load_default_certs includes Windows ROOT.
    else:
        assert calls[0]["cafile"] == transport.certifi.where()


@pytest.mark.parametrize("weaken", ["hostname", "certificate"])
def test_weakened_context_is_rejected(monkeypatch, weaken):
    context = ssl.create_default_context()
    context.check_hostname = False
    if weaken == "certificate":
        context.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(transport, "_shared_ssl_context", lambda: context)
    with pytest.raises(ValueError, match="certificate and hostname"):
        transport.http_client()
    with pytest.raises(ValueError, match="certificate and hostname"):
        transport.async_http_client()


def test_sync_sdk_and_copies_keep_verified_transport():
    with transport.create_openai_client(api_key="offline-test-placeholder", max_retries=0) as client:
        copied = client.with_options(timeout=5, max_retries=0)
        assert copied._client is client._client
        context = client._client._transport._pool._ssl_context
        assert context is transport.ssl_context()
        assert_verified(context)
        assert client._client.follow_redirects is False


def test_async_sdk_and_copies_keep_same_context_as_sync():
    async def scenario():
        async with transport.create_async_openai_client(api_key="offline-test-placeholder", max_retries=0) as client:
            copied = client.with_options(timeout=5, max_retries=0)
            assert copied._client is client._client
            context = client._client._transport._pool._ssl_context
            assert context is transport.ssl_context()
            assert_verified(context)
            assert client._client.follow_redirects is False
    asyncio.run(scenario())


@pytest.mark.parametrize("factory", [transport.create_openai_client, transport.create_async_openai_client])
def test_factories_reject_transport_override(factory):
    with pytest.raises(ValueError, match="central verified"):
        factory(api_key="offline-test-placeholder", http_client=None)


@pytest.mark.parametrize("name", ["agent_cli.py", "web_runtime.py", "live_smoke.py", "live_success_check.py",
                                  "tests/test_agent_live.py"])
def test_real_entrypoints_use_central_transport(name):
    import ast
    source = Path(__file__).resolve().parents[1] / name
    imports = [node for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))) if isinstance(node, ast.ImportFrom)]
    assert any(node.module == "openai_transport" and any(alias.name == "create_async_openai_client" for alias in node.names)
               for node in imports)
    assert not any(node.module == "openai" and any(alias.name in {"OpenAI", "AsyncOpenAI"} for alias in node.names)
                   for node in imports)
