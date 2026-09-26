import json

import httpx
import pytest
from conftest import TEST_KEY, item_row, monthly
from mcp import Client

import global_economic_statistical_mcp.ecos_client as client_module
from global_economic_statistical_mcp.server import mcp

pytestmark = pytest.mark.anyio

TOOLS = {"search_statistics", "get_metadata", "get_data", "compare_series", "calculate_statistics", "explain_indicator"}

OECD_CPI_DIMS = {
    "REF_AREA": "KOR", "FREQ": "M", "METHODOLOGY": "N", "MEASURE": "CPI", "UNIT_MEASURE": "IX",
    "EXPENDITURE": "_T", "ADJUSTMENT": "N", "TRANSFORMATION": "_Z",
}
IMF_CPI_DIMS = {"COUNTRY": "KOR", "INDEX_TYPE": "CPI", "COICOP_1999": "_T", "TYPE_OF_TRANSFORMATION": "IX", "FREQUENCY": "M"}


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


def add_korea_cpi(ecos, sdmx):
    """Same Korean CPI from three providers (OECD on a 2015 base, like the real data)."""
    ecos.add_series("901Y009", "M", monthly(2026, [118.03, 118.4, 118.8]))
    sdmx.add("IMF", "CPI", IMF_CPI_DIMS, {"2026-M01": 118.03, "2026-M02": 118.4, "2026-M03": 118.8},
             attrs={"REFERENCE_PERIOD": "2020A"}, name=None)
    factor = 124.26 / 118.03
    sdmx.add("OECD", "DSD_PRICES@DF_PRICES_ALL", OECD_CPI_DIMS,
             {p: round(v * factor, 4) for p, v in {"2026-01": 118.03, "2026-02": 118.4, "2026-03": 118.8}.items()},
             attrs={"BASE_PER": "2015"})


# ── Surface ─────────────────────────────────────────────────────────


async def test_server_surface():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
        prompts = (await c.list_prompts()).prompts
        resources = (await c.list_resources()).resources
    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations.read_only_hint and t.output_schema is None for t in tools)
    assert {p.name for p in prompts} == {"macro-economic-briefing", "compare-countries", "analyze-economic-trend"}
    assert {"gesm://concepts", "gesm://validation/summary", "gesm://providers"} <= {str(r.uri) for r in resources}


async def test_version_and_cli(capsys):
    from global_economic_statistical_mcp import __version__, main

    async with Client(mcp) as c:
        assert c.server_info.version == __version__ != ""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0 and __version__ in capsys.readouterr().out


# ── search / metadata ───────────────────────────────────────────────


async def test_search_across_catalogs():
    out = json.loads(await ok("search_statistics", {"query": "물가", "limit": 3}))
    assert out["concepts"][0]["concept_id"] == "CPI"
    assert "tables" in out and "items" in out
    flows = json.loads(await ok("search_statistics", {"query": "policy rates", "scope": "dataflows", "source": "BIS"}))
    assert flows["dataflows"]["data"][0]["dataflow"].startswith("BIS:WS_CBPOL")
    items = json.loads(await ok("search_statistics", {"query": "휘발유", "scope": "items", "limit": 5}))
    assert items["items"][0]["get_data_example"] == {"stat_code": "901Y009", "cycle": "M", "item_code1": "G02101"}


async def test_ecos_metadata_links_canonical_concepts(fake_ecos):
    fake_ecos.keyed[("StatisticItemList", "901Y009")] = [item_row("901Y009", 1, "0", "총지수", unit="2020=100")]
    out = json.loads(await ok("get_metadata", {"stat_code": "901Y009"}))
    assert out["provider"] == "ECOS" and out["series_key"] == ["FREQ", "ITEM_CODE1"]
    linked = {c["concept_id"]: c for c in out["canonical_concepts"]}
    assert set(linked) == {"CPI", "CPI_YOY"}
    assert {"provider": "IMF", "dataflow": "IMF.STA:CPI", "key": "KOR.CPI._T.IX.M", "freq": "M"} in linked["CPI"]["other_sources_for_KR"]


async def test_sdmx_metadata(fake_sdmx):
    fake_sdmx.structures["WS_CBPOL"] = {
        "data": {
            "dataflows": [{"id": "WS_CBPOL", "name": "Central bank policy rates"}],
            "dataStructures": [{"dataStructureComponents": {"dimensionList": {"dimensions": [
                {"id": "FREQ", "position": 0, "localRepresentation": {"enumeration": "urn:x=BIS:CL_FREQ(1.0)"}},
                {"id": "REF_AREA", "position": 1, "localRepresentation": {"enumeration": "urn:x=BIS:CL_AREA(1.0)"}},
            ]}}}],
            "codelists": [
                {"agencyID": "BIS", "id": "CL_FREQ", "version": "1.0", "codes": [{"id": "M", "name": "Monthly"}]},
                {"agencyID": "BIS", "id": "CL_AREA", "version": "1.0",
                 "codes": [{"id": "US", "name": "United States"}, {"id": "KR", "name": "Korea"}]},
            ],
        }
    }
    out = json.loads(await ok("get_metadata", {"source": "BIS", "dataflow": "BIS:WS_CBPOL(1.0)", "code_keyword": "korea"}))
    assert out["key_template"] == "{FREQ}.{REF_AREA}"
    area = out["dimensions"][1]
    assert area["total_codes"] == 2 and area["codes"] == [{"code": "KR", "name": "Korea"}]


# ── get_data ────────────────────────────────────────────────────────


async def test_concept_for_korea_uses_ecos_with_provenance_and_validation(fake_all):
    ecos, sdmx = fake_all
    add_korea_cpi(ecos, sdmx)
    out = json.loads(await ok("get_data", {"indicator": "소비자물가지수", "start_date": "2026-01", "end_date": "2026-03"}))
    assert out["concept"]["id"] == "CPI" and out["source"]["provider"] == "ECOS"
    series = out["series"][0]
    assert series["data"] == [["2026-01", 118.03], ["2026-02", 118.4], ["2026-03", 118.8]]
    assert (series["unit"], series["base_period"], series["country"]) == ("IX", "2020", "KR")
    assert out["provenance"][0]["citation"].startswith("출처: 한국은행")
    assert out["validation"][0]["status"] == "pass"


async def test_concept_for_another_country_uses_sdmx(fake_sdmx):
    fake_sdmx.add("BIS", "WS_CBPOL", {"FREQ": "M", "REF_AREA": "US"}, {"2026-01": 3.625, "2026-02": 3.625, "2026-03": 3.375})
    out = json.loads(await ok("get_data", {"indicator": "policy rate", "country": "US", "start_date": "2026-01", "end_date": "2026-03"}))
    assert out["source"] == {"provider": "BIS", "dataflow": "BIS:WS_CBPOL(1.0)", "key": "M.US", "freq": "M",
                             "note": "BIS central bank policy rates (monthly)"}
    series = out["series"][0]
    assert series["unit"] == "PC_PA" and series["unit_source"] == "catalog"
    assert out["validation"][0]["checks"]["unit"] == "info"


async def test_cross_validate_three_providers(fake_all):
    ecos, sdmx = fake_all
    add_korea_cpi(ecos, sdmx)
    out = json.loads(await ok("get_data", {"indicator": "CPI", "start_date": "2026-01", "end_date": "2026-03", "cross_validate": True}))
    cv = out["cross_validation"]
    assert cv["status"] in ("consistent", "consistent_within_tolerance")
    assert cv["method"] == "rebased:2026-01=100"  # OECD publishes on a 2015 base
    assert [s["provider"] for s in cv["sources"]] == ["ECOS", "IMF", "OECD"]
    assert cv["agreement"]["IMF"]["match"] == 3
    last = cv["records"][-1]
    assert last["values"]["ECOS"] == last["values"]["IMF"] and last["period"] == "2026-03"


async def test_policy_rate_changes_only_and_yoy_transform(fake_all):
    ecos, _ = fake_all
    values = {f"202601{d:02d}": 2.5 for d in range(1, 11)} | {f"202601{d:02d}": 2.75 for d in range(11, 21)}
    ecos.add_series("722Y001", "D", values, item_code1="0101000", item_name1="기준금리", unit="연%")
    out = json.loads(await ok("get_data", {"indicator": "기준금리", "start_date": "20260101", "end_date": "20260120"}))
    assert out["changes_only"] is True
    assert out["series"][0]["data"] == [["2026-01-01", 2.5], ["2026-01-11", 2.75], ["2026-01-20", 2.75]]
    # Validation ran on the full daily series, so no gaps are reported for the compressed view.
    assert out["validation"][0]["checks"]["missing"] == "pass"

    ecos.add_series("901Y009", "M", monthly(2025, [100] * 12 + [102, 103]), item_code1="A01101", item_name1="쌀")
    out = json.loads(await ok("get_data", {"stat_code": "901Y009", "item_code1": "A01101", "start_date": "2026-01", "end_date": "2026-02", "transform": "yoy"}))
    series = out["series"][0]
    assert series["columns"] == ["period", "value", "level"]
    assert series["data"] == [["2026-01", 2.0, 102], ["2026-02", 3.0, 103]]
    assert series["unit"] == "PC_YOY"


async def test_direct_sdmx_query_and_formats(fake_sdmx):
    dims = {"REF_AREA": "JPN", "FREQ": "M", "MEASURE": "IRLT", "UNIT_MEASURE": "PA", "ACTIVITY": "_Z",
            "ADJUSTMENT": "_Z", "TRANSFORMATION": "_Z", "TIME_HORIZ": "_Z", "METHODOLOGY": "N"}
    fake_sdmx.add("OECD", "DSD_STES@DF_FINMARK", dims, {"2026-01": 2.5, "2026-02": 2.6})
    args = {"source": "OECD", "dataflow": "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)", "key": "JPN.M.IRLT.PA._Z._Z._Z._Z.N",
            "cycle": "M", "country": "JP", "start_date": "2026-01", "end_date": "2026-02"}
    out = json.loads(await ok("get_data", args))
    assert out["series"][0]["unit"] == "PC_PA" and out["validation"][0]["checks"]["country"] == "pass"

    csv_text = await ok("get_data", {**args, "output_format": "csv"})
    assert "# source OECD:" in csv_text and "2026-02,2.6,PC_PA" in csv_text

    message = json.loads(await ok("get_data", {**args, "output_format": "sdmx"}))
    link = message["data"]["structures"][0]["links"][0]
    assert link["urn"] == "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)"

    full = json.loads(await ok("get_data", {**args, "output_format": "json"}))
    assert full["validation"][0]["checks"][0]["check"] == "country"


async def test_rebase_index(fake_all):
    ecos, _ = fake_all
    ecos.add_series("901Y009", "M", monthly(2026, [110.0, 121.0]))
    out = json.loads(await ok("get_data", {"indicator": "CPI", "start_date": "2026-01", "end_date": "2026-02", "rebase_period": "2026-01"}))
    assert out["series"][0]["data"] == [["2026-01", 100.0], ["2026-02", 110.0]]
    assert "rebase" in out["provenance"][0]["transformations"][0]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"indicator": "CPI", "transform": "cagr"}, "transform"),
        ({"indicator": "CPI", "cycle": "X"}, "주기"),
        ({"indicator": "CPI", "stat_code": "901Y009"}, "함께 쓸 수 없습니다"),
        ({"indicator": "M2", "country": "US"}, "출처가 없습니다"),
        ({"indicator": "CPI", "output_format": "xml"}, "output_format"),
        ({"indicator": "CPI", "start_date": "2026-13"}, "해석할 수 없습니다"),
    ],
)
async def test_invalid_requests_are_tool_errors(fake_all, args, message):
    is_error, text = await call("get_data", args)
    assert is_error and message in text


async def test_provider_errors_never_leak_the_ecos_key(fake_all):
    ecos, _ = fake_all
    ecos.forced.extend([httpx.Response(401)])
    is_error, text = await call("get_data", {"indicator": "CPI", "source": "ECOS"})
    assert is_error and "HTTP_401" in text and TEST_KEY not in text


# ── compare / statistics / explain ──────────────────────────────────


async def test_compare_countries_prefers_a_common_frequency(fake_all):
    ecos, sdmx = fake_all
    ecos.add_series("722Y001", "M", monthly(2026, [2.5, 2.5, 2.75]), item_code1="0101000", item_name1="기준금리", unit="연%")
    sdmx.add("BIS", "WS_CBPOL", {"FREQ": "M", "REF_AREA": "US"}, {"2026-01": 3.625, "2026-02": 3.625, "2026-03": 3.375})
    out = json.loads(await ok("compare_series", {
        "series": [{"indicator": "POLICY_RATE", "country": "KR"}, {"indicator": "POLICY_RATE", "country": "US"}],
        "start_date": "2026-01", "end_date": "2026-03",
    }))
    assert out["frequency"] == "M"
    assert out["series"][0]["series_id"] == "ECOS:722Y001:M.0101000"  # monthly ECOS mapping, not daily
    assert out["rows"] == [["2026-01", 2.5, 3.625], ["2026-02", 2.5, 3.625], ["2026-03", 2.75, 3.375]]
    assert "cross_validation" not in out  # different countries are compared, not cross-validated


async def test_compare_same_concept_across_providers_cross_validates(fake_all):
    ecos, sdmx = fake_all
    add_korea_cpi(ecos, sdmx)
    out = json.loads(await ok("compare_series", {
        "series": [{"indicator": "CPI", "source": "ECOS"}, {"indicator": "CPI", "source": "IMF"}],
        "start_date": "2026-01", "end_date": "2026-03",
    }))
    assert out["correlation"]["CPI:KR:ECOS ~ CPI:KR:IMF"]["r"] == 1.0
    assert out["cross_validation"][0]["status"] == "consistent"


async def test_calculate_statistics(fake_all):
    ecos, _ = fake_all
    ecos.add_series("901Y009", "M", monthly(2025, [100] * 12 + [100 + i for i in range(12)]))
    out = json.loads(await ok("calculate_statistics", {"indicator": "CPI", "start_date": "2025-01", "end_date": "2026-12"}))
    stats = out["series"][0]["stats"]
    assert stats["first"] == {"time": "2025-01", "value": 100} and stats["max"] == {"time": "2026-12", "value": 111}
    assert stats["latest_yoy_pct"] == 11.0
    assert out["series"][0]["validation"]["status"] == "pass"


async def test_explain_indicator(fake_all):
    ecos, _ = fake_all
    ecos.keyed[("StatisticWord", "소비자물가상승률")] = [{"WORD": "소비자물가상승률", "CONTENT": "전년동월대비 변화율"}]
    out = json.loads(await ok("explain_indicator", {"term": "CPI_YOY", "country": "US"}))
    assert out["concept"]["concept_id"] == "CPI_YOY"
    assert [s["provider"] for s in out["sources_for_country"]] == ["IMF", "OECD"]
    assert out["sources_for_country"][0]["key"] == "USA.CPI._T.YOY_PCH_PA_PT.M"
    assert out["definition"] == [{"word": "소비자물가상승률", "definition": "전년동월대비 변화율"}]
