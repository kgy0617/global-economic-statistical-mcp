"""Tests against the real ECOS API. Skipped by default; run with: uv run pytest -m live"""

import json
import urllib.request
from functools import cache

import pytest
from jsonschema import Draft201909Validator
from mcp import Client

from ecos_mcp.client import EcosClient
from ecos_mcp.config import POPULAR_INDICATORS
from ecos_mcp.server import mcp

pytestmark = [pytest.mark.live, pytest.mark.anyio]

# Official SDMX-JSON 2.1 schemas, pinned to a commit of github.com/sdmx-twg/sdmx-json
SDMX_JSON_COMMIT = "faa661d2247b9914052c76a5dabafd5990493f5a"
SCHEMA_URL = f"https://raw.githubusercontent.com/sdmx-twg/sdmx-json/{SDMX_JSON_COMMIT}/{{}}/tools/schemas/sdmx-json-{{}}-schema.json"


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
    assert not result.is_error, result.content[0].text
    return result.content[0].text


@pytest.mark.parametrize("preset", POPULAR_INDICATORS, ids=lambda p: p["id"])
async def test_every_preset_returns_data(preset):
    out = json.loads(await call("get_data", {"indicator": preset["id"], "recent_years": 1}))
    assert out["count"] > 0
    assert out["series"][0]["unit"] == preset["unit"].split(" ")[0]


@pytest.mark.parametrize(
    "args",
    [
        {"stat_code": "731Y004", "codes_limit": 5},  # two item groups
        {"stat_code": "404Y014", "item_keyword": "총지수"},  # '*AA' codes need escaping
    ],
)
async def test_metadata_is_valid_sdmx_structure(args):
    assert_valid_sdmx("structure", json.loads(await call("get_metadata", {**args, "output_format": "sdmx"})))


@pytest.mark.parametrize(
    "args",
    [
        {"indicator": "물가상승률"},
        {"indicator": "PPI"},
        {"stat_code": "731Y004", "cycle": "M", "item_code1": "0000001", "start_date": "2025"},
    ],
)
async def test_data_is_valid_sdmx_data_message(args):
    assert_valid_sdmx("data", json.loads(await call("get_data", {**args, "output_format": "sdmx"})))


async def test_analysis_tools():
    stats = json.loads(await call("calculate_statistics", {"indicator": "월평균환율"}))
    assert stats["series"][0]["stats"]["count"] > 0

    compared = json.loads(
        await call(
            "compare_series",
            {"series": [{"indicator": "월평균환율"}, {"indicator": "국고채월평균"}], "recent_years": 1},
        )
    )
    assert compared["frequency"] == "M" and compared["rows"]

    explained = json.loads(await call("explain_indicator", {"term": "경제심리지수"}))
    assert explained["definition"] and explained["methodology"] and explained["sdmx"]


async def test_key_statistics_and_search():
    out = json.loads(await call("search_statistics", {"scope": "key_statistics"}))
    assert len(out["key_statistics"]) >= 50

    client = EcosClient()
    try:
        assert (await client.search_statistic_word("기준금리", end_count=3))["count"] > 0
    finally:
        await client.close()
