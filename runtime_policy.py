"""Operator-only deployment policy; never populated from browser parameters."""
import os
from pathlib import Path
import tempfile
import math

from config import ROOT


def public_demo_only():
    # Render's own marker overrides accidentally copied local Live flags.
    return (os.environ.get("RENDER", "").lower() == "true"
            or os.environ.get("SMARTSUPPORT_PUBLIC_DEMO_ONLY") == "1")


def direct_demo_allowed(bind_address=None):
    """Allow password-free synthetic Demo sessions, never a remote Live session."""
    bind = bind_address or os.environ.get("SMARTSUPPORT_BIND_ADDRESS", "")
    if public_demo_only():
        return True
    return (os.environ.get("SMARTSUPPORT_ENABLE_LOCAL_LIVE") != "1"
            and bind in {"127.0.0.1", "::1", "localhost"})


def local_demo_bypass_allowed(bind_address=None):
    """Backward-compatible name for the password-free Demo policy."""
    return direct_demo_allowed(bind_address)


def web_data_directory():
    configured = os.environ.get("SMARTSUPPORT_WEB_DATA_DIR")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = ROOT / path
    elif public_demo_only():
        path = Path(tempfile.gettempdir()) / "smartsupport-ai"
    else:
        path = ROOT / "data" / "web_demo"
    return path.resolve()


def worker_limit():
    try:
        return max(1, min(4, int(os.environ.get("SMARTSUPPORT_MAX_WORKERS", "4"))))
    except ValueError:
        return 1


def visitor_idle_timeout():
    """Operator-only visitor idle TTL, seconds; invalid settings fail closed."""
    seconds = float(os.environ.get("SMARTSUPPORT_VISITOR_IDLE_SECONDS", "1800"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("SMARTSUPPORT_VISITOR_IDLE_SECONDS must be positive and finite")
    return seconds
