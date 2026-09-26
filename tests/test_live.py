"""Tests against the real ECOS, OECD, IMF and BIS APIs. Skipped by default; run with:

    uv run pytest -m live

Every Concept Catalog mapping is re-verified here for Korea and the United States, so a
provider changing a dataflow or key is caught instead of silently returning nothing.
"""

import json
import urllib.request
from functools import cache

import pytest
from jsonschema import Draft201909Validator
from mcp import Client

from global_economic_statistical_mcp.catalog.concepts import CONCEPTS
from global_economic_statistical_mcp.catalog.countries import get_country
from global_economic_statistical_mcp.config import get_default_date_range
from global_economic_statistical_mcp.ecos_client import EcosClient
from global_economic_statistical_mcp.model import canonical_unit, ecos_to_canonical
from global_economic_statistical_mcp.providers.base import ProviderError
from global_economic_statistical_mcp.server import mcp
from global_economic_statistical_mcp.service import ResolvedSource, StatService

pytestmark = [pytest.mark.live, pytest.mark.anyio]

# Official SDMX-JSON 2.1 schemas, pinned to a commit of github.com/sdmx-twg/sdmx-json
SDMX_JSON_COMMIT = "faa661d2247b9914052c76a5dabafd5990493f5a"
SCHEMA_URL = f"https://raw.githubusercontent.com/sdmx-twg/sdmx-json/{SDMX_JSON_COMMIT}/{{}}/tools/schemas/sdmx-json-{{}}-schema.json"

MAPPINGS = [
    (concept, mapping, country)
    for concept in CONCEPTS
    for country in ("KR", "US")
    for mapping in concept.sources_for(get_country(country))
    # OECD does not publish a US-dollar rate for the United States itself.
    if not (concept.id == "USD_EXCHANGE_RATE" and country == "US" and mapping.provider == "OECD")
]


@cache
def sdmx_validator(kind: str) -> Draft201909Validator:
    url = SCHEMA_URL.format(f"{kind}-message", kind)
    with urllib.request.urlopen(url, timeout=30) as response:
        schema = json.load(response)
    # 'prepared' is oneOf[date-time, date]; formats must be asserted for it to be decidable.
    return Draft201909Validator(schema, format_checker=Draft201909Validator.FORMAT_CHECKER)


def assert_valid_sdmx(kind: str, document: dict) -> None:
    errors = [f"{list(e.absolute_path)}: {e.message[:200]}" for e in sdmx_validator(kind).iter_errors(document)]
    assert not errors, errors[:5]


async def call(name, args):
    async with Client(mcp) as c:
        result = await c.call_tool(name, args)
    text = result.content[0].text
    if result.is_error and "RATE_LIMITED" in text:
        pytest.skip(f"provider rate limit (not a code error): {text[:120]}")
    assert not result.is_error, text
    return text


@pytest.fixture
async def service():
    s = StatService(ecos_client=EcosClient())
    yield s
    await s.close()


@pytest.mark.parametrize(
    ("concept", "mapping", "country"),
    MAPPINGS,
    ids=[f"{c.id}-{m.provider}-{m.freq}-{k}" for c, m, k in MAPPINGS],
)
async def test_catalog_mapping_returns_valid_data(service, concept, mapping, country):
    ctry = get_country(country)
    start, end = get_default_date_range(mapping.freq, recent_years=1)
    src = ResolvedSource(
        mapping.provider, mapping.dataflow, mapping.render_key(ctry), mapping.freq, concept, ctry, mapping,
        mapping.transform, False, concept.name_ko,
    )
    try:
        loaded = await service.load(src, ecos_to_canonical(start, mapping.freq), ecos_to_canonical(end, mapping.freq), end_count=10, record=False)
    except ProviderError as e:
        if e.code == "RATE_LIMITED":
            pytest.skip(f"{e.provider} rate limit (not a mapping error): {e.message}")
        raise
    assert len(loaded.series) == 1, [s.series_id for s in loaded.series]
    series, report = loaded.series[0], loaded.reports[0]
    assert series.observations, "no observations"
    failed = [c for c in report.checks if c.status == "fail"]
    assert not failed, failed
    assert series.ref_area == country
    if series.unit:
        assert canonical_unit(series.unit) in (mapping.unit, "PC", "XDC"), (series.unit, mapping.unit)


async def test_cross_validation_of_korean_cpi_agrees():
    out = json.loads(await call("get_data", {"indicator": "CPI", "country": "KR", "recent_years": 1, "cross_validate": True}))
    cv = out["cross_validation"]
    assert cv["status"] in ("consistent", "consistent_within_tolerance"), cv
    assert cv["agreement"]["IMF"]["mismatch"] == 0


@pytest.mark.parametrize(
    "args",
    [
        {"indicator": "CPI_YOY", "country": "KR"},  # ECOS, transformed on the server
        {"indicator": "LONG_TERM_RATE", "country": "US"},  # OECD
        {"indicator": "POLICY_RATE", "country": "JP"},  # BIS
    ],
)
async def test_data_output_is_valid_sdmx_json(args):
    assert_valid_sdmx("data", json.loads(await call("get_data", {**args, "recent_years": 1, "output_format": "sdmx"})))


async def test_ecos_structure_is_valid_sdmx_json():
    assert_valid_sdmx("structure", json.loads(await call("get_metadata", {"stat_code": "731Y004", "codes_limit": 5, "output_format": "sdmx"})))


@pytest.mark.parametrize(
    ("source", "dataflow"),
    [("OECD", "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)"), ("IMF", "IMF.STA:CPI"), ("BIS", "BIS:WS_XRU(1.0)")],
)
async def test_sdmx_structures_resolve_codelists(source, dataflow):
    out = json.loads(await call("get_metadata", {"source": source, "dataflow": dataflow, "codes_limit": 3}))
    assert out["dimensions"] and all(d["codes"] for d in out["dimensions"]), out["dimensions"]
