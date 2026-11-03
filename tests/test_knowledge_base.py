import json

import pytest

from knowledge_base import DEFAULT_FAQ_PATH, search_faq


def test_faq_count_and_unique_sources():
    data = json.loads(DEFAULT_FAQ_PATH.read_text(encoding="utf-8"))
    assert data["synthetic"] is True
    assert len(data["items"]) == 20
    assert len({item["id"] for item in data["items"]}) == 20


@pytest.mark.parametrize("query,expected", [
    ("كم تستغرق مدة الشحن؟", "FAQ-001"),
    ("ما سياسة إرجاع المنتجات؟", "FAQ-002"),
    ("ما مدة الضمان؟", "FAQ-011"),
    ("كيف أعرف مواصفات الشاشة؟", "FAQ-016"),
])
def test_faq_search_returns_sourced_answer(query, expected):
    results = search_faq(query)
    assert results[0]["id"] == expected
    assert results[0]["answer"]
    assert len(results) <= 3


def test_arabic_diacritics_are_normalized():
    assert search_faq("مُدَّة الضَّمَان")[0]["id"] == "FAQ-011"


def test_unknown_query_has_no_fabricated_answer():
    assert search_faq("xyzunknown987") == []


@pytest.mark.parametrize("query", ["", "   ", None, "x" * 1001])
def test_invalid_faq_query(query):
    with pytest.raises(ValueError):
        search_faq(query)
