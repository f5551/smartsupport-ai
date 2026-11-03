"""Shared, verified TLS for OpenAI only; no network or credential loading on import.

Windows uses its existing trusted roots (including an administrator-approved
HTTPS inspection root). Other platforms retain HTTPX's certifi trust bundle.
This module never imports certificates into an OS store or disables verification.
Restart the process after trusted certificates change to rebuild the context.
"""

from functools import lru_cache
import ssl
import sys
from threading import Lock

import certifi
from openai import AsyncOpenAI, OpenAI, DefaultAsyncHttpxClient, DefaultHttpxClient

_context_lock = Lock()


@lru_cache(maxsize=1)
def _shared_ssl_context():
    if sys.platform == "win32":
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    else:
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=certifi.where())
    return context


def ssl_context():
    # Serialize cache initialization across concurrent UI workers too.
    with _context_lock:
        context = _shared_ssl_context()
    # Fail closed if another caller has accidentally weakened the shared context.
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("OpenAI TLS requires certificate and hostname verification")
    return context


def http_client():
    return DefaultHttpxClient(verify=ssl_context(), follow_redirects=False)


def async_http_client():
    return DefaultAsyncHttpxClient(verify=ssl_context(), follow_redirects=False)


def create_openai_client(**options):
    if "http_client" in options:
        raise ValueError("Use the central verified OpenAI HTTP transport")
    transport = http_client()
    try:
        return OpenAI(http_client=transport, **options)
    except BaseException:
        transport.close()
        raise


def create_async_openai_client(**options):
    if "http_client" in options:
        raise ValueError("Use the central verified OpenAI HTTP transport")
    return AsyncOpenAI(http_client=async_http_client(), **options)
