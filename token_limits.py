"""Offline o200k input estimate and bounded, valid JSON tool results.

The vocabulary is vendored from OpenAI's tiktoken CDN and hash-checked. JSON
tokens + framing reserve are a local estimate, not provider-measured usage.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import tiktoken
from tiktoken.load import load_tiktoken_bpe

VOCAB = Path(__file__).resolve().parent / "assets/tokenizer/o200k_base.tiktoken"
VOCAB_HASH = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"


@lru_cache(maxsize=1)
def encoding():
    if hashlib.sha256(VOCAB.read_bytes()).hexdigest() != VOCAB_HASH:
        raise ValueError("Local tokenizer vocabulary unavailable or invalid")
    # o200k_base pattern from the installed tiktoken (MIT) implementation.
    pattern = "|".join([
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
    ])
    return tiktoken.Encoding(name="smartsupport_o200k", pat_str=pattern,
        mergeable_ranks=load_tiktoken_bpe(str(VOCAB)), special_tokens={"<|endoftext|>":199999,"<|endofprompt|>":200018})


def estimate_input(instructions, history, tools):
    text = json.dumps({"instructions": instructions, "input": history, "tools": tools},
                      ensure_ascii=False, separators=(",", ":"))
    return len(encoding().encode(text, disallowed_special=())) + 256 + 8 * len(history) + 16 * len(tools)


def bounded_history(message, history, limit):
    cleaned = []
    for item in history:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
            raise ValueError("History must contain user/assistant text only")
        if len(item["content"]) > 8000:
            raise ValueError("History message too large")
        cleaned.append({"role": item["role"], "content": item["content"]})
    cleaned = cleaned[-(limit - 1):] if limit > 1 else []
    while cleaned and cleaned[0]["role"] == "assistant":
        cleaned.pop(0)
    return cleaned + [{"role": "user", "content": message}]


def compact_result(result, limit):
    envelope = {"trust": "untrusted_store_data", "result": deepcopy(result)}
    def dump():
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if len(dump()) <= limit:
        return dump()
    envelope["truncated"] = True
    envelope["notice"] = "بيانات مختصرة؛ لا تستنتج الأجزاء المحذوفة."
    data = envelope["result"].get("data")
    # FAQ policy entries remain complete: omit whole lower-ranked matches.
    if isinstance(data, list):
        omitted = 0
        while data and len(dump()) > limit:
            data.pop()
            omitted += 1
        envelope["omitted_items"] = omitted
        if data and len(dump()) <= limit:
            return dump()
    elif isinstance(data, dict):
        # Keep receipt/status/ownership fields intact; explicit previews only.
        for field in ("items_json", "description", "content"):
            if field in data and len(dump()) > limit:
                value = data[field]
                envelope.setdefault("truncated_fields", []).append(field)
                data[field] = ""
                if isinstance(value, str):
                    room = max(0, limit - len(dump()) - 8)
                    data[field] = value[:room] + ("…" if room else "")
        if len(dump()) <= limit:
            return dump()
    # Never emit broken JSON or a partially cut policy/identifier/error code.
    return json.dumps({"trust":"untrusted_store_data","result":{"ok":False,"error":{
        "code":"RESULT_TOO_LARGE","message":"نتيجة الأداة لا تتسع؛ اطلب استعلامًا أضيق ولا تخترع الأجزاء المحذوفة."}}}, ensure_ascii=False, separators=(",", ":"))
