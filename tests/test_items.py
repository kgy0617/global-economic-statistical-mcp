"""Tests for the item and commodity catalog (items.py)."""

from __future__ import annotations

from ecos_mcp.items import STATISTICAL_ITEMS, search_items


def test_items_catalog_populated():
    assert len(STATISTICAL_ITEMS) >= 30
    names = {it["item_name"] for it in STATISTICAL_ITEMS}
    assert "쌀" in names
    assert "휘발유" in names
    assert "전기료" in names
    assert "사과" in names


def test_search_items_rice():
    hits = search_items("쌀")
    assert len(hits) >= 2
    # Should find 쌀 in CPI (901Y009) and PPI (404Y014)
    stat_codes = {h["stat_code"] for h in hits}
    assert "901Y009" in stat_codes
    for h in hits:
        assert "get_data_example" in h
        assert h["get_data_example"]["stat_code"] in ("901Y009", "404Y014")


def test_search_items_gasoline():
    hits = search_items("휘발유")
    assert len(hits) >= 1
    assert any(h["item_name"] == "휘발유" and h["stat_code"] == "901Y009" for h in hits)


def test_search_items_semiconductor():
    hits = search_items("반도체")
    assert len(hits) >= 1
    assert any("DRAM" in h["item_name"] or "반도체" in h["item_name"] for h in hits)


def test_search_items_english_synonym():
    hits = search_items("apple")
    assert len(hits) >= 1
    assert any(h["item_name"] == "사과" for h in hits)
