"""Consistency of the Concept Catalog and the generated search catalogs."""

import pytest

from global_economic_statistical_mcp.catalog import search
from global_economic_statistical_mcp.catalog.concepts import (
    CONCEPTS,
    find_concept,
    get_concept,
    rank_concepts,
)
from global_economic_statistical_mcp.catalog.countries import (
    DEFAULT_COUNTRIES,
    get_country,
    iso2,
)
from global_economic_statistical_mcp.config import VALID_CYCLES
from global_economic_statistical_mcp.model import UNIT_LABELS
from global_economic_statistical_mcp.providers.sdmx_rest import SOURCES, parse_flow_ref


def test_concepts_are_well_formed():
    ids = [c.id for c in CONCEPTS]
    assert len(ids) == len(set(ids))
    owners: dict[str, set[str]] = {}
    for c in CONCEPTS:
        for a in (c.id, *c.aliases):
            owners.setdefault(a.upper(), set()).add(c.id)
    assert all(len(v) == 1 for v in owners.values()), {k: v for k, v in owners.items() if len(v) > 1}
    korea = get_country("KR")
    for c in CONCEPTS:
        assert c.unit in UNIT_LABELS, c.id
        assert c.sources, c.id
        for s in c.sources:
            assert s.provider in ("ECOS", *SOURCES), (c.id, s)
            assert s.freq in VALID_CYCLES, (c.id, s)
            assert s.transform in (None, "yoy", "pop")
            if s.provider == "ECOS":
                assert s.countries == ("KR",), "ECOS only covers Korea"
            else:
                parse_flow_ref(s.dataflow)
                assert "{" in s.key, "international keys must be templated by country"
                for country in DEFAULT_COUNTRIES:
                    assert "{" not in s.render_key(get_country(country)), (c.id, country)
            assert "{" not in s.render_key(korea)


def test_international_dataflows_exist_in_provider_catalog():
    """Every SDMX dataflow used by a concept is listed in the generated dataflows.json."""
    catalog = search._load("dataflows.json")["providers"]
    for c in CONCEPTS:
        for s in c.sources:
            if s.provider == "ECOS":
                continue
            prefix = s.dataflow.split("(")[0]
            assert any(f["ref"].split("(")[0] == prefix for f in catalog[s.provider]), (c.id, s.dataflow)


def test_ecos_concept_tables_exist_in_item_index_when_indexed():
    indexed = {t["stat_code"]: {i["code"] for i in t["items"]} for t in search._load("ecos_items.json")["tables"]}
    for c in CONCEPTS:
        for s in c.sources:
            if s.provider == "ECOS" and s.dataflow in indexed:
                first_code = s.key.split(".")[0]
                assert first_code in indexed[s.dataflow], (c.id, s.dataflow, first_code)


def test_item_synonyms_point_at_real_item_names():
    names = {i["name"] for t in search._load("ecos_items.json")["tables"] for i in t["items"]}
    missing = [n for n in search.ITEM_SYNONYMS if n not in names]
    assert not missing, f"synonyms for names not in the ECOS index: {missing}"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("CPI_YOY", "CPI_YOY"),
        ("inflation", "CPI_YOY"),
        ("물가상승률", "CPI_YOY"),
        ("물가", "CPI"),
        ("기준금리", "POLICY_RATE"),
        ("base_rate", "POLICY_RATE"),  # former preset id
        ("policy rate", "POLICY_RATE"),
        ("최근 기준금리 추이", "POLICY_RATE"),
        ("환율", "USD_EXCHANGE_RATE"),
        ("usd_krw", "USD_EXCHANGE_RATE_DAILY"),
        ("실업률", "UNEMPLOYMENT_RATE"),
        ("성장률", "GDP_REAL_GROWTH_QOQ"),
        ("10년물", "LONG_TERM_RATE"),
        ("집값", "HOUSE_PRICE_INDEX"),
    ],
)
def test_find_concept(query, expected):
    concept = find_concept(query)
    assert concept is not None and concept.id == expected


@pytest.mark.parametrize("query", ["", "   ", "존재하지않는지표xyz"])
def test_find_concept_rejects_unknown(query):
    assert find_concept(query) is None


def test_ambiguous_query_is_not_guessed():
    assert find_concept("지수") is None
    assert len(rank_concepts("지수")) > 1


def test_sources_for_country_and_filters():
    cpi = get_concept("CPI")
    kr = [s.provider for s in cpi.sources_for(get_country("KR"))]
    us = [s.provider for s in cpi.sources_for(get_country("US"))]
    assert kr[0] == "ECOS" and "ECOS" not in us
    assert [s.provider for s in cpi.sources_for(get_country("US"), provider="oecd")] == ["OECD"]
    assert get_concept("POLICY_RATE").sources_for(get_country("KR"), freq="M")[0].provider == "ECOS"


def test_countries():
    assert iso2("KOR") == "KR" and iso2("usa") == "US" and iso2("XX") == "XX"
    assert {iso2(c) for c in ("XM", "G163", "EA20", "EA", "eurozone", "유로존")} == {"EA"}
    assert get_country("UK").iso2 == "GB"
    assert get_country("한국").iso3 == "KOR"
    assert get_country("japan").currency == "JPY"


def test_item_search_uses_generated_codes():
    rice = search.search_items("쌀", limit=1)[0]
    assert (rice["stat_code"], rice["item_code"]) == ("901Y009", "A01101")
    assert search.search_items("계란")[0]["item_name"] == "달걀"
    gasoline = search.search_items("gasoline")
    assert {g["stat_code"] for g in gasoline} >= {"901Y009", "404Y014"}


def test_dataflow_search():
    res = search.search_dataflows("policy rates", provider="BIS")
    assert any(r["dataflow"].startswith("BIS:WS_CBPOL") for r in res["data"])
    assert search.dataflow_name("IMF", "IMF.STA:CPI") == "Consumer Price Index (CPI)"


def test_every_default_economy_has_international_coverage_of_core_concepts():
    """Research use needs the core macro concepts for every default economy (re-verified by the live suite)."""
    core = (
        "POLICY_RATE", "LONG_TERM_RATE", "SHORT_TERM_RATE", "CPI", "CPI_YOY", "GDP_REAL_GROWTH_QOQ", "GDP_REAL_GROWTH_YOY",
        "GDP_REAL", "GDP_NOMINAL", "CURRENT_ACCOUNT", "GOODS_BALANCE", "FX_RESERVES", "SHARE_PRICE_INDEX",
        "CONSUMER_SENTIMENT", "BUSINESS_CONFIDENCE", "HOUSE_PRICE_INDEX",
    )
    missing = [(c.id, k) for c in CONCEPTS if c.id in core for k in DEFAULT_COUNTRIES if not c.sources_for(get_country(k))]
    assert not missing, missing


def test_flows_are_summed_and_stocks_take_the_period_end():
    assert {c.id for c in CONCEPTS if c.aggregation == "sum"} == {"GDP_REAL", "GDP_NOMINAL", "CURRENT_ACCOUNT", "GOODS_BALANCE"}
    assert {c.id for c in CONCEPTS if c.aggregation == "last"} == {"FX_RESERVES"}
