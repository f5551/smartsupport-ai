"""Phase-2 validation and write guard; authentication/consent UI is future work.

Never populate identity or approval from a model response. No credentials are
stored here. Tool outputs, FAQ and memory are untrusted data, not instructions.
"""

import re


class WriteApprovalRequired(PermissionError):
    """An explicit trusted-caller approval is required before a write."""


def require_write_approval(approved: bool) -> None:
    if approved is not True:
        raise WriteApprovalRequired("Explicit application approval is required")


def validate_text(value: str, name: str, max_length: int) -> str:
    """SQL safety uses bound parameters, not escaping or banning punctuation."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value) > max_length or "\x00" in value:
        raise ValueError(f"{name} is invalid or too long")
    return value


def contains_untrusted_instruction(value) -> bool:
    """Telemetry heuristic only; authority checks do not depend on this detector.

    We never log the matching text. Absence of a match is NOT proof of safety.
    """
    if isinstance(value, dict):
        return any(contains_untrusted_instruction(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_untrusted_instruction(v) for v in value)
    return isinstance(value, str) and bool(re.search(
        r"ignore.{0,40}(?:instructions|system)|system\s*:|تجاهل.{0,40}(?:تعليمات|النظام)|مدير النظام|approved\s*=\s*true",
        value, flags=re.IGNORECASE | re.DOTALL))
