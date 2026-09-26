import json

import httpx
import pytest
from conftest import TEST_KEY, item_row, monthly
from mcp import Client

import ecos_mcp.client as client_module
from ecos_mcp.server import mcp

pytestmark = pytest.mark.anyio

TOOLS = {
    "search_statistics",
    "get_metadata",
    "get_data",
    "compare_series",
    "calculate_statistics",
    "explain_indicator",
}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(client_module, "ECOS_API_KEY", TEST_KEY)


async def call(name, args):
    async with Client(mcp) as c:
        result = await c.call_tool(name, args)
    return result.is_error, result.content[0].text


async def ok(name, args):
    is_error, text = await call(name, args)
    assert not is_error, text
    return text


# ── Server surface ──────────────────────────────────────────────────


async def test_server_surface():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
        prompts = (await c.list_prompts()).prompts
        resources = (await c.list_resources()).resources
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.output_schema is None  # no duplicated structuredContent
    assert {p.name for p in prompts} == {"macro-economic-briefing", "analyze-economic-trend"}
    assert "ecos://sdmx/conventions" in {str(r.uri) for r in resources}


async def test_server_reports_package_version():
    from ecos_mcp import __version__

    async with Client(mcp) as c:
        assert c.server_info.version == __version__ != ""


def test_cli_version_and_unknown_args(capsys):
    from ecos_mcp import __version__, main

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out

    with pytest.raises(SystemExit) as excinfo:
        main(["--bogus"])
    assert excinfo.value.code == 2


# ── search_statistics ───────────────────────────────────────────────


async def test_search_returns_indicators_and_tables():
    out = json.loads(await ok("search_statistics", {"query": "소비자 물가", "limit": 2}))
    assert {i["id"] for i in out["indicators"]} == {"cpi", "inflation"}
    assert out["tables"]["data"][0]["STAT_CODE"] == "901Y009"
    assert out["tables"]["total_matches"] >= 2


async def test_search_browses_table_tree():
    root = json.loads(await ok("search_statistics", {"scope": "tables"}))
    assert all(t["P_STAT_CODE"] == "*" for t in root["tables"]["data"])
    first = root["tables"]["data"][0]["STAT_CODE"]
    child = json.loads(await ok("search_statistics", {"scope": "tables", "parent_code": first}))
    assert child["parent"]["STAT_CODE"] == first

    is_error, _ = await call("search_statistics", {"scope": "tables", "parent_code": "NOPE"})
    assert is_error


async def test_search_key_statistics(fake_ecos):
    fake_ecos.lists["KeyStatisticList"] = [
        {"CLASS_NAME": "환율", "KEYSTAT_NAME": "원/달러 환율(종가)", "DATA_VALUE": "1366", "CYCLE": "20260924", "UNIT_NAME": "원"},
        {"CLASS_NAME": "금리", "KEYSTAT_NAME": "한국은행 기준금리", "DATA_VALUE": "2.5", "CYCLE": "20260924", "UNIT_NAME": "%"},
    ]
    out = json.loads(await ok("search_statistics", {"query": "환율", "scope": "key_statistics"}))
    assert out["key_statistics"] == [
        {"class": "환율", "name": "원/달러 환율(종가)", "value": 1366, "unit": "원", "time": "20260924"}
    ]


async def test_search_rejects_unknown_scope():
    is_error, text = await call("search_statistics", {"scope": "everything"})
    assert is_error and "scope" in text


# ── get_metadata ────────────────────────────────────────────────────


FX_ITEMS = [
    item_row("731Y004", 1, "0000001", "원/미국달러(매매기준율)", cycle="M"),
    item_row("731Y004", 1, "0000001", "원/미국달러(매매기준율)", cycle="A", start="1964", end="2025"),
    item_row("731Y004", 1, "0000002", "원/일본엔(100엔)", cycle="M"),
    item_row("731Y004", 2, "0000100", "평균자료", cycle="M"),
    item_row("731Y004", 2, "0000200", "말일자료", cycle="M"),
]


async def test_metadata_compact_maps_item_groups_to_dimensions(fake_ecos):
    fake_ecos.keyed[("StatisticItemList", "731Y004")] = FX_ITEMS
    out = json.loads(await ok("get_metadata", {"stat_code": "731Y004"}))
    assert out["series_key"] == ["FREQ", "ITEM_CODE1", "ITEM_CODE2"]
    freq, item1, item2 = out["dimensions"]
    assert [c["code"] for c in freq["codes"]] == ["M", "A"]
    assert item1["name"] == "계정항목" and item1["total_codes"] == 2
    assert item1["codes"][0]["availability"] == {"M": "200001~202608", "A": "1964~2025"}
    assert item2["name"] == "측정항목"
    assert out["get_data_example"] == {
        "stat_code": "731Y004", "cycle": "M", "item_code1": "0000001", "item_code2": "0000100"
    }


async def test_metadata_filters_codes(fake_ecos):
    fake_ecos.keyed[("StatisticItemList", "731Y004")] = FX_ITEMS
    out = json.loads(await ok("get_metadata", {"stat_code": "731Y004", "item_keyword": "엔"}))
    assert [c["code"] for c in out["dimensions"][1]["codes"]] == ["0000002"]
    assert out["partial"] is True


async def test_metadata_sdmx_structure_message(fake_ecos):
    fake_ecos.keyed[("StatisticItemList", "404Y014")] = [item_row("404Y014", 1, "*AA", "총지수", unit="2020=100")]
    out = json.loads(await ok("get_metadata", {"stat_code": "404Y014", "output_format": "sdmx"}))
    data = out["data"]
    assert data["dataflows"][0]["id"] == "404Y014"
    dims = data["dataStructures"][0]["dataStructureComponents"]["dimensionList"]["dimensions"]
    assert [d["id"] for d in dims] == ["FREQ", "ITEM_CODE1"]
    item_codes = next(c for c in data["codelists"] if c["id"].endswith("ITEM_CODE1"))["codes"]
    assert item_codes[0]["id"] == "$2AAA"  # '*' is not allowed in SDMX ids
    assert {"type": "ECOS_ITEM_CODE", "title": "*AA"} in item_codes[0]["annotations"]


async def test_metadata_rejects_category_codes():
    is_error, text = await call("get_metadata", {"stat_code": "0000000001"})
    assert is_error and "분류" in text


# ── get_data ────────────────────────────────────────────────────────


async def test_base_rate_returns_only_changes(fake_ecos):
    values = {f"202401{d:02d}": 3.5 for d in range(1, 11)}
    values.update({f"202401{d:02d}": 3.25 for d in range(11, 21)})
    fake_ecos.add_series("722Y001", "D", values, item_code1="0101000", item_name1="기준금리", unit="연%")
    out = json.loads(
        await ok("get_data", {"indicator": "기준금리", "start_date": "20240101", "end_date": "20240120"})
    )
    assert out["changes_only"] is True
    assert out["series"][0]["data"] == [["20240101", 3.5], ["20240111", 3.25], ["20240120", 3.25]]


async def test_inflation_preset_computes_yoy(fake_ecos):
    fake_ecos.add_series("901Y009", "M", monthly(2023, [100] * 12 + [102] * 12))
    out = json.loads(
        await ok("get_data", {"indicator": "물가상승률", "start_date": "202401", "end_date": "202403"})
    )
    assert out["columns"] == ["time", "value", "yoy_pct"]
    assert out["series"][0]["data"] == [["202401", 102, 2.0], ["202402", 102, 2.0], ["202403", 102, 2.0]]
    assert out["count"] == 3


async def test_truncated_yoy_fetches_base_year(fake_ecos, monkeypatch):
    monkeypatch.setattr(client_module, "ECOS_API_KEY", "sample")
    fake_ecos.add_series("901Y009", "M", monthly(2022, [100] * 24 + [110] * 12))
    out = json.loads(
        await ok(
            "get_data",
            {"stat_code": "901Y009", "cycle": "M", "start_date": "202301", "end_date": "202412",
             "item_code1": "0", "transform": "yoy"},
        )
    )
    assert out["truncated"] is True
    # Counts describe the requested range only, not the base year fetched for yoy.
    assert out["total_count"] == 24 and "전체 24건" in out["note"]
    data = out["series"][0]["data"]
    assert data[-1] == ["202412", 110, 10.0]
    assert all(point[2] is not None for point in data)


async def test_transform_bases_follow_the_requested_page(fake_ecos):
    fake_ecos.add_series("901Y009", "M", monthly(2022, [100] * 12 + [105] * 12 + [110] * 12))
    out = json.loads(
        await ok(
            "get_data",
            {"stat_code": "901Y009", "cycle": "M", "start_date": "202301", "end_date": "202412",
             "transform": "yoy", "prefer_latest": False, "start_count": 11, "end_count": 15},
        )
    )
    data = out["series"][0]["data"]
    assert [p[0] for p in data] == ["202311", "202312", "202401", "202402", "202403"]
    assert [p[2] for p in data] == [5.0, 5.0, 4.76, 4.76, 4.76]


async def test_pop_uses_observation_before_start(fake_ecos):
    fake_ecos.add_series("200Y102", "Q", {"2023Q4": 100, "2024Q1": 110, "2024Q2": 121})
    out = json.loads(
        await ok(
            "get_data",
            {"stat_code": "200Y102", "cycle": "Q", "start_date": "2024Q1", "end_date": "2024Q2", "transform": "pop"},
        )
    )
    assert out["total_count"] == 2
    assert out["series"][0]["data"] == [["2024Q1", 110, 10.0], ["2024Q2", 121, 10.0]]


async def test_flexible_dates_and_default_cycle(fake_ecos):
    fake_ecos.add_series("901Y009", "M", monthly(2024, list(range(1, 13))))
    # No cycle: taken from the table index (901Y009 is monthly). '2024' spans the whole year.
    out = json.loads(await ok("get_data", {"stat_code": "901Y009", "start_date": "2024", "end_date": "2024"}))
    assert (out["cycle"], out["start_date"], out["end_date"]) == ("M", "202401", "202412")
    assert out["count"] == 12


async def test_csv_output(fake_ecos):
    fake_ecos.add_series(
        "200Y102", "Q", {"2024Q1": 1.3, "2024Q2": -0.2}, item_code1="10111",
        item_name1="국내총생산(GDP)(실질, 계절조정, 전기비)", unit="%",
    )
    text = await ok(
        "get_data",
        {"indicator": "성장률", "start_date": "2024Q1", "end_date": "2024Q2", "output_format": "csv"},
    )
    assert '2024Q2,10111,"국내총생산(GDP)(실질, 계절조정, 전기비)",-0.2,%' in text


async def test_sdmx_data_message(fake_ecos):
    fake_ecos.add_series("901Y009", "M", monthly(2023, [100] * 12 + [102] * 3))
    out = json.loads(
        await ok(
            "get_data",
            {"indicator": "물가상승률", "start_date": "202401", "end_date": "202403", "output_format": "sdmx"},
        )
    )
    structure = out["data"]["structures"][0]
    assert [d["id"] for d in structure["dimensions"]["series"]] == ["FREQ", "ITEM_CODE1"]
    assert [v["value"] for v in structure["dimensions"]["observation"][0]["values"]] == [
        "2024-01", "2024-02", "2024-03"
    ]
    assert [m["id"] for m in structure["measures"]["observation"]] == ["OBS_VALUE", "YOY_PCT"]
    observations = out["data"]["dataSets"][0]["series"]["0:0"]["observations"]
    assert observations["0"] == [102, 2.0]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"stat_code": "901Y009", "cycle": "X"}, "유효하지 않은 주기"),
        ({"stat_code": "901Y009", "cycle": "M", "start_date": "2024-13"}, "해석할 수 없습니다"),
        ({"stat_code": "901Y009", "cycle": "M", "start_date": "202405", "end_date": "202401"}, "늦습니다"),
        ({"stat_code": "901Y009", "cycle": "D", "transform": "yoy"}, "yoy"),
        ({"stat_code": "901Y009", "cycle": "M", "transform": "cagr"}, "transform"),
        ({"stat_code": "901Y009", "cycle": "M", "output_format": "xml"}, "output_format"),
        ({"indicator": "기준금리", "stat_code": "722Y001"}, "함께 쓸 수 없습니다"),
        ({"indicator": "환율", "cycle": "M"}, "고정 지표"),
        ({}, "indicator 또는 stat_code"),
        ({"indicator": "통화"}, "reserve_money"),
    ],
)
async def test_invalid_arguments_are_tool_errors(fake_ecos, args, message):
    is_error, text = await call("get_data", args)
    assert is_error and message in text
    assert fake_ecos.calls == []


async def test_api_errors_are_tool_errors_without_key(fake_ecos):
    fake_ecos.forced.append(httpx.Response(401))
    is_error, text = await call("get_data", {"stat_code": "901Y009", "cycle": "M"})
    assert is_error and "HTTP_401" in text and TEST_KEY not in text


# ── compare_series ──────────────────────────────────────────────────


async def test_compare_aligns_frequencies_and_correlates(fake_ecos):
    # Daily rate stepping up each month vs a monthly index moving in lockstep.
    daily = {}
    for month in range(1, 7):
        for day in (1, 15):
            daily[f"2024{month:02d}{day:02d}"] = month
    fake_ecos.add_series("722Y001", "D", daily, item_code1="0101000", item_name1="기준금리", unit="연%")
    fake_ecos.add_series("901Y009", "M", monthly(2024, [100 + 2 * m for m in range(1, 7)]))
    out = json.loads(
        await ok(
            "compare_series",
            {
                "series": [
                    {"stat_code": "722Y001", "cycle": "D", "item_code1": "0101000", "label": "rate"},
                    {"stat_code": "901Y009", "cycle": "M", "item_code1": "0", "label": "cpi"},
                ],
                "start_date": "2024-01",
                "end_date": "2024-06",
            },
        )
    )
    assert out["frequency"] == "M"
    assert out["columns"] == ["time", "rate", "cpi"]
    assert out["rows"][0] == ["202401", 1, 102]
    assert len(out["rows"]) == 6
    assert out["correlation"]["rate ~ cpi"] == {"r": 1.0, "n": 6}


async def test_compare_normalizes_to_index(fake_ecos):
    fake_ecos.add_series("901Y009", "M", monthly(2024, [50, 100]))
    fake_ecos.add_series("404Y014", "M", monthly(2024, [200, 100]), item_code1="*AA")
    out = json.loads(
        await ok(
            "compare_series",
            {
                "series": [
                    {"stat_code": "901Y009", "cycle": "M", "label": "a"},
                    {"stat_code": "404Y014", "cycle": "M", "label": "b"},
                ],
                "start_date": "202401",
                "end_date": "202402",
                "normalize_method": "index",
            },
        )
    )
    assert out["rows"] == [["202401", 100.0, 100.0], ["202402", 200.0, 50.0]]


async def test_compare_rejects_upsampling_and_ambiguous_series(fake_ecos):
    is_error, text = await call(
        "compare_series",
        {"series": [{"indicator": "성장률"}, {"indicator": "물가"}], "frequency": "M"},
    )
    assert is_error and "변환할 수 없습니다" in text

    fake_ecos.add_series("901Y009", "M", monthly(2024, [1, 2]), item_code1="0")
    fake_ecos.add_series("901Y009", "M", monthly(2024, [3, 4]), item_code1="A", item_name1="식료품")
    is_error, text = await call(
        "compare_series",
        {"series": [{"stat_code": "901Y009", "cycle": "M"}, {"indicator": "물가"}], "start_date": "2024"},
    )
    assert is_error and "item_code" in text


# ── calculate_statistics ────────────────────────────────────────────


async def test_calculate_statistics(fake_ecos):
    fake_ecos.add_series("901Y009", "M", monthly(2023, [100] * 12 + [100 + i for i in range(12)]))
    out = json.loads(
        await ok("calculate_statistics", {"indicator": "CPI", "start_date": "2024-01", "end_date": "2024-12"})
    )
    stats = out["series"][0]["stats"]
    assert stats["count"] == 12
    assert stats["first"] == {"time": "202401", "value": 100}
    assert stats["max"] == {"time": "202412", "value": 111}
    assert stats["change_pct"] == 11.0
    assert stats["latest_yoy_pct"] == 11.0
    assert stats["trend_per_year"] == pytest.approx(12.0)
    assert stats["trend_r2"] == 1.0


# ── explain_indicator ───────────────────────────────────────────────


async def test_explain_combines_glossary_meta_and_structure(fake_ecos):
    fake_ecos.keyed[("StatisticWord", "경제심리지수")] = [{"WORD": "경제심리지수(ESI)", "CONTENT": "민간의 경제심리"}]
    fake_ecos.keyed[("StatisticMeta", "경제심리지수")] = [
        {"LVL": "1", "CONT_NAME": "기본정보", "META_DATA": None},
        {"LVL": "2", "CONT_NAME": "담당기관", "META_DATA": "한국은행"},
    ]
    fake_ecos.keyed[("StatisticItemList", "513Y001")] = [item_row("513Y001", 1, "E1000", "경제심리지수(원계열)", unit="")]
    out = json.loads(await ok("explain_indicator", {"term": "경제심리지수"}))
    assert out["definition"] == [{"word": "경제심리지수(ESI)", "definition": "민간의 경제심리"}]
    assert out["methodology"] == [{"dataset": "경제심리지수"}, {"section": "담당기관", "text": "한국은행"}]
    assert out["tables"][0]["STAT_CODE"] == "513Y001"
    assert out["sdmx"]["dataflow"]["id"] == "513Y001"
    assert out["sdmx"]["series_key"] == ["FREQ", "ITEM_CODE1"]


async def test_explain_unknown_term(fake_ecos):
    is_error, text = await call("explain_indicator", {"term": "존재하지않는용어xyz"})
    assert is_error and "찾지 못했습니다" in text


# ── Semantic Search & Canonical Concepts & Harmonization ──────────────


async def test_search_statistics_sub_items_rice(fake_ecos):
    out = json.loads(await ok("search_statistics", {"query": "쌀"}))
    assert "items" in out
    assert len(out["items"]) >= 1
    rice = next((i for i in out["items"] if i["item_name"] == "쌀"), None)
    assert rice is not None
    assert rice["stat_code"] == "901Y009"
    assert rice["item_code"] == "01111"
    assert "get_data_example" in rice


async def test_search_statistics_concepts_inflation(fake_ecos):
    out = json.loads(await ok("search_statistics", {"query": "inflation"}))
    assert "concepts" in out
    concept_ids = {c["concept_id"] for c in out["concepts"]}
    assert "CPI_INFLATION_RATE" in concept_ids or "CPI_HEADLINE" in concept_ids


async def test_get_data_canonical_concept_and_evidence(fake_ecos):
    fake_ecos.add_series(
        "722Y001",
        "D",
        {
            "20240101": 3.5,
            "20240115": 3.5,
            "20240201": 3.25,
        },
        item_code1="0101000",
        unit="연%",
    )
    out = json.loads(
        await ok(
            "get_data",
            {"indicator": "BOK_BASE_RATE", "start_date": "20240101", "end_date": "20240210"},
        )
    )
    assert "evidence" in out
    ev = out["evidence"]
    assert "한국은행" in ev["source_agency"]
    assert ev["table_code"] == "722Y001"
    assert ev["sdmx_concept"] == "BOK_BASE_RATE"
    assert "https://ecos.bok.or.kr/#/Search/722Y001" in ev["ecos_url"]
    assert "citation" in ev


async def test_get_data_unit_mult_scaling(fake_ecos):
    # GDP table 200Y108 in billions (십억원)
    fake_ecos.add_series(
        "200Y108",
        "Q",
        {"2024Q1": 500.0, "2024Q2": 520.0},
        item_code1="10601",
        unit="십억원",
    )
    # Scale from 십억원 (unit_mult=9) to 백만원 (unit_mult=6) -> x1000
    out = json.loads(
        await ok(
            "get_data",
            {
                "stat_code": "200Y108",
                "cycle": "Q",
                "item_code1": "10601",
                "start_date": "2024Q1",
                "end_date": "2024Q2",
                "unit_mult": 6,
            },
        )
    )
    series_data = out["series"][0]["data"]
    # 500 * 10^3 = 500,000
    assert series_data[0][1] == 500000.0
    assert out["evidence"]["unit_mult"] == 6


async def test_get_data_rebase_period(fake_ecos):
    fake_ecos.add_series(
        "901Y009",
        "M",
        {"202401": 110.0, "202402": 121.0, "202403": 132.0},
        item_code1="0",
        unit="2020=100",
    )
    out = json.loads(
        await ok(
            "get_data",
            {
                "stat_code": "901Y009",
                "cycle": "M",
                "item_code1": "0",
                "start_date": "202401",
                "end_date": "202403",
                "rebase_period": "202401",
            },
        )
    )
    series_data = out["series"][0]["data"]
    # 110 becomes 100.0, 121 becomes 110.0
    assert series_data[0][1] == 100.0
    assert series_data[1][1] == 110.0


async def test_compare_series_rebase_and_evidence(fake_ecos):
    fake_ecos.add_series("901Y009", "M", {"202401": 100.0, "202402": 110.0}, item_code1="0")
    fake_ecos.add_series("731Y004", "M", {"202401": 1300.0, "202402": 1430.0}, item_code1="0000001")

    out = json.loads(
        await ok(
            "compare_series",
            {
                "series": [
                    {"stat_code": "901Y009", "cycle": "M", "item_code1": "0", "label": "CPI"},
                    {"stat_code": "731Y004", "cycle": "M", "item_code1": "0000001", "label": "USD"},
                ],
                "start_date": "202401",
                "end_date": "202402",
                "normalize_method": "rebase",
                "rebase_period": "202401",
            },
        )
    )
    # Both start at 100.0
    assert out["rows"][0][1] == 100.0
    assert out["rows"][0][2] == 100.0
    assert out["rows"][1][1] == 110.0
    assert out["rows"][1][2] == 110.0
    assert "evidence" in out
    assert len(out["evidence"]) == 2
    assert "출처: 한국은행" in out["evidence"][0]["citation"]

