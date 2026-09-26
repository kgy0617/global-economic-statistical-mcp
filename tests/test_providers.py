"""SDMX and ECOS adapters: provider dialects → CanonicalSeries."""

import pytest
from conftest import TEST_KEY, monthly

from global_economic_statistical_mcp.ecos_client import EcosClient
from global_economic_statistical_mcp.providers.base import ProviderError, SeriesRequest
from global_economic_statistical_mcp.providers.ecos import EcosProvider, ecos_unit
from global_economic_statistical_mcp.providers.sdmx_rest import (
    SOURCES,
    SdmxHttp,
    SdmxProvider,
    data_url,
    parse_data_message,
    parse_flow_ref,
    summarize_structure,
)

pytestmark = pytest.mark.anyio


# ── URLs and references ─────────────────────────────────────────────


def test_parse_flow_ref():
    assert parse_flow_ref("OECD.SDD.TPS:DSD_PRICES@DF_PRICES_ALL(1.0)") == ("OECD.SDD.TPS", "DSD_PRICES@DF_PRICES_ALL", "1.0")
    assert parse_flow_ref("IMF.STA:CPI") == ("IMF.STA", "CPI", "latest")
    with pytest.raises(ProviderError):
        parse_flow_ref("not a flow")


def test_data_urls_per_dialect():
    oecd = data_url(SOURCES["OECD"], "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)", "KOR.M.IRLT", "2026-01", "2026-08")
    assert oecd == (
        "https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0/KOR.M.IRLT"
        "?startPeriod=2026-01&endPeriod=2026-08&dimensionAtObservation=AllDimensions"
    )
    imf = data_url(SOURCES["IMF"], "IMF.STA:CPI", "KOR.CPI._T.IX.M", "2026-01", "2026-08")
    assert imf == (
        "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/KOR.CPI._T.IX.M"
        "?c%5BTIME_PERIOD%5D=ge:2026-01+le:2026-08&dimensionAtObservation=AllDimensions"
    )
    assert data_url(SOURCES["BIS"], "BIS:WS_CBPOL(1.0)", "M.KR", None, None).startswith(
        "https://stats.bis.org/api/v1/data/BIS,WS_CBPOL,1.0/M.KR?"
    )


# ── Parsing ─────────────────────────────────────────────────────────


def test_parse_json2_with_literal_attribute_values():
    doc = {
        "data": {
            "structures": [
                {
                    "name": {"en": "Flow"},
                    "dimensions": {"observation": [
                        {"id": "COUNTRY", "values": [{"id": "KOR"}]},
                        {"id": "TIME_PERIOD", "values": [{"value": "2026-M01"}]},
                    ]},
                    "attributes": {"observation": [{"id": "REFERENCE_PERIOD", "values": []}]},
                }
            ],
            "dataSets": [{"structure": 0, "observations": {"0:0": ["120.05", "2020A"]}}],
        }
    }
    rows, _names, name = parse_data_message(doc)
    assert name == "Flow"
    assert rows == [{"COUNTRY": "KOR", "TIME_PERIOD": "2026-M01", "_value": "120.05", "@REFERENCE_PERIOD": "2020A"}]


async def _fetch(fake, provider, flow, key, freq="M", start="2026-01", end="2026-12"):
    http = SdmxHttp(max_retries=0)
    try:
        return await SdmxProvider(SOURCES[provider], http).fetch(SeriesRequest(provider, flow, key, freq, start, end))
    finally:
        await http.close()


async def test_oecd_unit_from_transformation(fake_sdmx):
    flow = "OECD.SDD.TPS:DSD_PRICES@DF_PRICES_ALL(1.0)"
    fake_sdmx.add("OECD", "DSD_PRICES@DF_PRICES_ALL",
                  {"REF_AREA": "KOR", "FREQ": "M", "UNIT_MEASURE": "PA", "TRANSFORMATION": "GY", "ADJUSTMENT": "N"},
                  {"2026-01": 2.0, "2026-02": 2.1}, attrs={"UNIT_MULT": "0"})
    [series] = await _fetch(fake_sdmx, "OECD", flow, "KOR.M.PA.GY.N")
    # UNIT_MEASURE=PA with TRANSFORMATION=GY is a year-on-year change, not an interest rate.
    assert (series.unit, series.ref_area, series.adjustment) == ("PC_YOY", "KR", "NSA")
    assert [(o.period, o.value) for o in series.observations] == [("2026-01", 2.0), ("2026-02", 2.1)]
    assert series.provenance.query_url.startswith("https://sdmx.oecd.org/")


async def test_imf_periods_strings_and_base_period(fake_sdmx):
    fake_sdmx.add("IMF", "CPI", {"COUNTRY": "KOR", "INDEX_TYPE": "CPI", "COICOP_1999": "_T", "TYPE_OF_TRANSFORMATION": "IX", "FREQUENCY": "M"},
                  {"2026-M01": 118.03, "2026-M02": 118.4}, attrs={"REFERENCE_PERIOD": "2020A"}, name=None)
    [series] = await _fetch(fake_sdmx, "IMF", "IMF.STA:CPI", "KOR.CPI._T.IX.M")
    assert series.observations[0].period == "2026-01" and series.observations[0].value == 118.03
    assert (series.unit, series.base_period, series.freq) == ("IX", "2020", "M")
    assert series.title.startswith("Consumer Price Index (CPI)")  # name from the dataflow catalog


async def test_provider_ignoring_wildcard_key_is_post_filtered(fake_sdmx):
    fake_sdmx.ignore_wildcard_keys = True
    dims = {"COUNTRY": "KOR", "INDEX_TYPE": "CPI", "COICOP_1999": "_T", "TYPE_OF_TRANSFORMATION": "IX", "FREQUENCY": "M"}
    fake_sdmx.add("IMF", "CPI", dims, {"2026-M01": 118.0})
    fake_sdmx.add("IMF", "CPI", {**dims, "COUNTRY": "AGO"}, {"2026-M01": 280.0})
    series = await _fetch(fake_sdmx, "IMF", "IMF.STA:CPI", "KOR.CPI._T.*.M")
    assert [s.ref_area for s in series] == ["KR"]
    assert "다른 관측치 1건" in series[0].notes[0]


async def test_bis_json1_and_implied_index_base(fake_sdmx):
    fake_sdmx.add("BIS", "WS_SPP", {"FREQ": "Q", "REF_AREA": "KR", "VALUE": "N", "UNIT_MEASURE": "628"}, {"2026-Q1": 145.0})
    [series] = await _fetch(fake_sdmx, "BIS", "BIS:WS_SPP(1.0)", "Q.KR.N.628", freq="Q", start="2026-Q1", end="2026-Q2")
    assert (series.unit, series.base_period, series.freq) == ("IX", "2010", "Q")


async def test_no_results_is_empty_and_client_errors_raise(fake_sdmx):
    assert await _fetch(fake_sdmx, "BIS", "BIS:WS_CBPOL(1.0)", "M.ZZ") == []
    import httpx

    fake_sdmx.forced.append(httpx.Response(400, text="bad key"))
    with pytest.raises(ProviderError) as excinfo:
        await _fetch(fake_sdmx, "BIS", "BIS:WS_CBPOL(1.0)", "X")
    assert excinfo.value.code == "HTTP_400"


def test_structure_summary_matches_version_ranges():
    doc = {
        "data": {
            "dataflows": [{"id": "OTHER", "name": "Wrong"}, {"id": "CPI", "name": "Consumer Price Index (CPI)"}],
            "dataStructures": [{"dataStructureComponents": {"dimensionList": {"dimensions": [
                {"id": "COUNTRY", "position": 0,
                 "conceptIdentity": "urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=IMF:CS_MASTER_DATA(1.0+.0).COUNTRY"}
            ]}}}],
            "conceptSchemes": [{"agencyID": "IMF", "id": "CS_MASTER_DATA", "version": "1.0.0", "concepts": [
                {"id": "COUNTRY", "name": "Country",
                 "coreRepresentation": {"enumeration": "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=IMF:CL_COUNTRY(1.6+.0)"}}
            ]}],
            "codelists": [{"agencyID": "IMF", "id": "CL_COUNTRY", "version": "1.6.0",
                           "codes": [{"id": "KOR", "names": {"en": "Korea, Republic of"}}]}],
        }
    }
    summary = summarize_structure(doc, "IMF.STA:CPI", "url")
    assert summary["name"] == "Consumer Price Index (CPI)"
    assert summary["dimensions"][0]["codes"] == [{"code": "KOR", "name": "Korea, Republic of"}]
    assert summary["dimensions"][0]["name"] == "Country"


# ── ECOS adapter ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("연%", ("PC_PA", 0, None)),
        ("십억원", ("XDC", 9, None)),
        ("천달러", ("USD", 3, None)),
        ("2020=100", ("IX", 0, "2020")),
        ("1980.01.04=100 ", ("IX", 0, "19800104")),
        (None, (None, 0, None)),
    ],
)
def test_ecos_unit(label, expected):
    assert ecos_unit(label) == expected


async def test_ecos_adapter_produces_canonical_series(fake_ecos):
    fake_ecos.add_series("731Y004", "M", monthly(2026, [1450.0, 1460.0]), item_code1="0000001",
                         item_name1="원/미국달러", unit="원", item_code2="0000100", item_name2="평균자료")
    provider = EcosProvider(EcosClient(api_key=TEST_KEY, retry_backoff=0))
    try:
        [series] = await provider.fetch(SeriesRequest("ECOS", "731Y004", "0000001.0000100", "M", "2026-01", "2026-02"))
    finally:
        await provider.close()
    assert series.series_id == "ECOS:731Y004:M.0000001.0000100"
    assert (series.unit, series.ref_area, series.unit_label) == ("XDC", "KR", "원")
    assert [(o.period, o.value) for o in series.observations] == [("2026-01", 1450.0), ("2026-02", 1460.0)]
    assert TEST_KEY not in series.provenance.query_url and "{API_KEY}" in series.provenance.query_url


async def test_rate_limit_opens_a_circuit_and_fails_fast(fake_sdmx):
    import httpx

    http = SdmxHttp(max_retries=2)
    provider = SdmxProvider(SOURCES["OECD"], http)
    request = SeriesRequest("OECD", "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)", "USA.M.IRLT", "M", "2026-01", "2026-02")
    fake_sdmx.forced.append(httpx.Response(429, headers={"Retry-After": "30"}, text="quota"))
    try:
        with pytest.raises(ProviderError) as first:
            await provider.fetch(request)
        calls = len(fake_sdmx.calls)
        with pytest.raises(ProviderError) as second:
            await provider.fetch(request)
    finally:
        await http.close()
    assert first.value.code == second.value.code == "RATE_LIMITED"
    assert len(fake_sdmx.calls) == calls == 1  # no retry storm, and the second call never left the process
