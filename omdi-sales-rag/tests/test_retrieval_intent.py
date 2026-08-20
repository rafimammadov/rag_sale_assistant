from __future__ import annotations

from app.services.retrieval import (
    build_fts_query,
    expand_retrieval_query,
    fold_for_matching,
    is_catalog_overview_query,
)


def test_turkish_ascii_catalog_question_is_detected() -> None:
    query = "Elinizde hangi urunler var?"

    assert is_catalog_overview_query(query)
    expanded = expand_retrieval_query(query)
    assert "product families" in expanded
    assert "ürün grupları" in expanded


def test_english_catalog_question_is_detected() -> None:
    assert is_catalog_overview_query("What products do you offer?")
    assert is_catalog_overview_query("Show me your catalog")


def test_specific_product_question_is_not_catalog_overview() -> None:
    query = "What is the price of Y6131?"

    assert not is_catalog_overview_query(query)
    assert expand_retrieval_query(query) == query


def test_turkish_characters_are_folded_for_intent_matching() -> None:
    assert fold_for_matching("ÜRÜN FİYATI") == "urun fiyati"


def test_expanded_fts_query_contains_category_terms() -> None:
    value = build_fts_query(expand_retrieval_query("Hangi ürünler var?"))

    assert '"ürünler"*' in value
    assert '"product"*' in value
