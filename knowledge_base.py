"""Local FAQ search without models, embeddings or content execution."""

import json
from pathlib import Path
import re
import unicodedata

from security import validate_text

DEFAULT_FAQ_PATH = Path(__file__).resolve().parent / "data" / "faq.json"
STOP_WORDS = {"ما", "هي", "هو", "هل", "كيف", "كم", "من", "في", "عن", "على", "و", "او", "الي"}


def _tokens(text: str) -> set[str]:
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    text = text.casefold().replace("ـ", "").replace("ى", "ي")
    words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    return {word[2:] if word.startswith("ال") and len(word) > 3 else word
            for word in words if word not in STOP_WORDS}


def search_faq(query: str, *, faq_path=DEFAULT_FAQ_PATH) -> list[dict]:
    """Return up to 3 ranked sourced matches or [] if no words match.

    Uses normalized word overlap with question, tags and answer. This is a
    lexical baseline, not semantic understanding or prompt injection protection.
    """
    query = validate_text(query, "query", 1000)
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    payload = json.loads(Path(faq_path).read_text(encoding="utf-8"))
    ranked = []
    for item in payload["items"]:
        score = (3 * len(query_tokens & _tokens(item["question"]))
                 + 4 * len(query_tokens & _tokens(" ".join(item["tags"])))
                 + len(query_tokens & _tokens(item["answer"])))
        if score:
            ranked.append((score, item["id"], item))
    ranked.sort(key=lambda match: (-match[0], match[1]))
    return [{"id": item["id"], "question": item["question"], "answer": item["answer"]}
            for _, _, item in ranked[:3]]
