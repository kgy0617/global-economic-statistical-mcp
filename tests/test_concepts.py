"""Tests for the canonical SDMX concept layer (concepts.py)."""

from __future__ import annotations

from ecos_mcp.concepts import (
    CONCEPTS,
    all_concepts,
    get_concept,
    search_concepts,
)


def test_concepts_registry_populated():
    concepts = all_concepts()
    assert len(concepts) >= 20
    ids = {c["concept_id"] for c in concepts}
    assert "BOK_BASE_RATE" in ids
    assert "CPI_HEADLINE" in ids
    assert "GDP_REAL_GROWTH" in ids
    assert "EXR_USD_KRW_DAILY" in ids
    assert "MONEY_M2" in ids
    assert "BOND_KTB_3Y_DAILY" in ids


def test_get_concept_exact_and_case_insensitive():
    c1 = get_concept("BOK_BASE_RATE")
    assert c1 is not None
    assert c1.name_ko == "한국은행 기준금리"
    assert c1.sdmx.ref_area == "KR"
    assert c1.sdmx.unit_measure == "PC_PA"

    c2 = get_concept("bok_base_rate")
    assert c2 is not None
    assert c2.concept_id == "BOK_BASE_RATE"

    c3 = get_concept("NON_EXISTENT_CONCEPT")
    assert c3 is None


def test_search_concepts_korean_and_english():
    hits_ko = search_concepts("기준금리")
    assert len(hits_ko) >= 1
    assert hits_ko[0]["concept_id"] == "BOK_BASE_RATE"

    hits_en = search_concepts("inflation")
    assert len(hits_en) >= 1
    assert any(h["concept_id"] in ("CPI_INFLATION_RATE", "CPI_HEADLINE") for h in hits_en)

    hits_gdp = search_concepts("gdp")
    assert len(hits_gdp) >= 1
    assert any("GDP" in h["concept_id"] for h in hits_gdp)


def test_cross_agency_mappings_present():
    cpi = get_concept("CPI_HEADLINE")
    assert cpi is not None
    assert cpi.cross_agency.imf.get("dataflow") == "IFS"
    assert cpi.cross_agency.oecd.get("dataflow") == "PRICES_CPI"

    rate = get_concept("BOK_BASE_RATE")
    assert rate is not None
    assert rate.cross_agency.bis.get("dataflow") == "CBPOL"
