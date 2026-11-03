"""Stable operation identity shared by approval and business-store ledgers."""
import hashlib
import json
import re


def canonical_payload(arguments):
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(arguments):
    return hashlib.sha256(canonical_payload(arguments).encode("utf-8")).hexdigest()


def idempotency_key(customer_id, session_id, tool, arguments):
    material = json.dumps(["smartsupport-write-v1", customer_id, session_id, tool,
                           canonical_payload(arguments)], ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def valid_idempotency_key(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
