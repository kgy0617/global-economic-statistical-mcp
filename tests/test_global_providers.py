"""ECB (SDMX 2.1), Eurostat (SDMX 3.0, SDMX-CSV) and World Bank (Data360, not SDMX) against fakes."""

import json

import pytest
from conftest import TEST_KEY
from test_server import call, ok

from global_economic_statistical_mcp.analytics import convert_frequency
from global_economic_statistical_mcp.catalog.concepts import get_concept
from global_economic_statistical_mcp.catalog.countries import get_country
from global_economic_statistical_mcp.ecos_client import EcosClient
from global_economic_statistical_mcp.providers import data360
from global_economic_statistical_mcp.providers.sdmx_rest import (
    SOURCES,
    data_url,
    parse_csv_message,
    summarize_structure_xml,
)
from global_economic_statistical_mcp.service import StatService, canonical_provider

pytestmark = pytest.mark.anyio


@pytest.fixture
async def service(fake_all):
    s = StatService(ecos_client=EcosClient(api_key=TEST_KEY, retry_backoff=0))
    s.http.max_retries = 0
    yield s
    await s.close()


def test_provider_aliases():
    assert [canonical_provider(x) for x in ("World Bank", "worldbank", "Data360", "ESTAT", "eurostat", "ecb", None)] == [
        "WB", "WB", "WB", "EUROSTAT", "EUROSTAT", "ECB", None,
    ]


def test_eurostat_keys_ask_for_wildcards_where_sdmx3_allows_one_value():
    url = data_url(SOURCES["EUROSTAT"], "ESTAT:UNE_RT_M(1.0)", "M.SA..PC_ACT.T.EA21+DE", "2026-01", "2026-06")
    assert "/UNE_RT_M/1.0/M.SA.*.PC_ACT.T.*?" in url and "ge:2026-01+le:2026-06" in url


def test_sdmx_csv_versions_parse_to_the_same_rows():
    csv2 = "STRUCTURE,STRUCTURE_ID,freq,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG\r\ndataflow,ESTAT:X(1.0),M,EA21,2026-01,6.3,p\r\n"
    csv1 = "DATAFLOW,LAST UPDATE,freq,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG\nESTAT:X(1.0),22/09/26,M,EA21,2026-01,6.3,p\n"
    expected = [{"FREQ": "M", "GEO": "EA21", "TIME_PERIOD": "2026-01", "_value": "6.3", "@OBS_FLAG": "p"}]
    assert parse_csv_message(csv2) == parse_csv_message(csv1) == expected


async def test_eurostat_gzip_csv_becomes_a_validated_euro_area_series(service, fake_all):
    _, sdmx = fake_all
    sdmx.add("EUROSTAT", "UNE_RT_M", {"FREQ": "M", "S_ADJ": "NSA", "AGE": "TOTAL", "UNIT": "PC_ACT", "SEX": "T", "GEO": "EA21"},
             {"2026-01": 6.3, "2026-02": 6.4})
    src = service.resolve(indicator="UNEMPLOYMENT_RATE", country="EA", source="Eurostat")[0]
    loaded = await service.load(src, "2026-01", "2026-02")
    series, report = loaded.series[0], loaded.reports[0]
    assert (series.ref_area, series.unit, series.adjustment) == ("EA", "PC", "NSA")
    assert [o.value for o in series.observations] == [6.3, 6.4]
    assert report.status in ("pass", "info")


async def test_ecb_hicp_reads_units_and_the_2025_base(service, fake_all):
    _, sdmx = fake_all
    dims = {"FREQ": "M", "REF_AREA": "U2", "ADJUSTMENT": "N", "ICP_ITEM": "000000", "DATA_PROVIDER": "4D0"}
    sdmx.add("ECB", "HICP", {**dims, "ICP_SUFFIX": "INX"}, {"2026-07": 103.24, "2026-08": 103.69},
             attrs={"UNIT": "IX", "UNIT_INDEX_BASE": "2025 = 100"})
    sdmx.add("ECB", "HICP", {**dims, "ICP_SUFFIX": "ANR"}, {"2026-07": 3.0, "2026-08": 3.2}, attrs={"UNIT": "PCCH"})
    cpi = (await service.load(service.resolve(indicator="CPI", country="EA", source="ECB")[0], "2026-07", "2026-08")).series[0]
    yoy = (await service.load(service.resolve(indicator="CPI_YOY", country="EA", source="ECB")[0], "2026-07", "2026-08")).series[0]
    assert (cpi.ref_area, cpi.unit, cpi.base_period) == ("EA", "IX", "2025")
    assert yoy.unit == "PC_YOY"


async def test_ecb_exchange_rate_is_inverted_and_its_area_declared(service, fake_all):
    _, sdmx = fake_all
    sdmx.add("ECB", "EXR", {"FREQ": "M", "CURRENCY": "USD", "CURRENCY_DENOM": "EUR", "EXR_TYPE": "SP00", "EXR_SUFFIX": "A"},
             {"2026-08": 1.25}, attrs={"UNIT": "USD"})
    loaded = await service.load(service.resolve(indicator="USD_EXCHANGE_RATE", country="EA", source="ECB")[0], "2026-08", "2026-08")
    series, report = loaded.series[0], loaded.reports[0]
    assert (series.observations[0].value, series.observations[0].source_value, series.unit) == (0.8, 1.25, "XDC_USD")
    assert series.ref_area == "EA" and series.ref_area_declared
    assert {c.check: c.status for c in report.checks}["country"] == "info"
    assert any(t.startswith("invert:") for t in series.provenance.transformations)
    assert any("declared by the catalog" in t for t in series.provenance.transformations)


async def test_world_bank_series_pages_and_units(service, fake_all, monkeypatch):
    _, sdmx = fake_all
    monkeypatch.setattr(data360, "_PAGE", 2)  # force several pages
    sdmx.wb.add("WB_WDI_NY_GDP_MKTP_KD_ZG", "EMU", {"2022": 3.6, "2023": 0.4, "2024": 0.9, "2025": 1.4},
                name="GDP growth (annual %)")
    loaded = await service.load(service.resolve(indicator="GDP_REAL_GROWTH_ANNUAL", country="EA")[0], "2022", "2025")
    series = loaded.series[0]
    assert (series.provider, series.ref_area, series.unit, series.freq) == ("WB", "EA", "PC_YOY", "A")
    assert [o.period for o in series.observations] == ["2022", "2023", "2024", "2025"]
    assert series.title.startswith("GDP growth (annual %)")
    assert series.provenance.agency == "World Bank" and "data360.worldbank.org" in series.provenance.web_url
    assert sum("/data?" in c for c in sdmx.wb.calls) == 2  # 4 rows, 2 per page


async def test_annual_source_is_compared_separately_on_complete_years(service, fake_all):
    ecos, sdmx = fake_all
    quarters = {"2024Q1": 100.0, "2024Q2": 100.0, "2024Q3": 100.0, "2024Q4": 100.0, "2025Q1": 110.0}
    ecos.add_series("200Y108", "Q", quarters, item_code1="10601", unit="십억원")
    sdmx.add("IMF", "QNEA", {"COUNTRY": "KOR", "INDICATOR": "B1GQ", "PRICE_TYPE": "Q", "S_ADJUSTMENT": "SA",
                             "TYPE_OF_TRANSFORMATION": "XDC", "FREQUENCY": "Q"},
             {"2024-Q1": 100e9, "2024-Q2": 100e9, "2024-Q3": 100e9, "2024-Q4": 100e9, "2025-Q1": 110e9})
    sdmx.wb.add("WB_WDI_NY_GDP_MKTP_KN", "KOR", {"2024": 400e9, "2025": 999e9}, unit="XDC_K", name="GDP (constant LCU)")

    def window(src):
        return {"Q": ("2024-Q1", "2025-Q1"), "A": ("2024", "2025")}[src.freq]

    result = await service.cross_check(get_concept("GDP_REAL"), get_country("KR"), window)
    assert result["frequency"] == "Q" and result["agreement"]["IMF"]["validation_status"] == "MATCH"
    annual = result["other_frequencies"][0]
    assert annual["frequency"] == "A" and annual["validation_status"] == "MATCH"
    # 2025 has one quarter only, so it is not compared with the World Bank's full year.
    assert result["agreement"]["WB"]["match"] == 1


def test_incomplete_periods_are_not_aggregated():
    points = [("202401", 1.0), ("202402", 1.0), ("202403", 1.0), ("202404", 1.0)]
    assert convert_frequency(points, "M", "Q", "sum") == [("2024Q1", 3.0), ("2024Q2", 1.0)]
    assert convert_frequency(points, "M", "Q", "sum", complete_only=True) == [("2024Q1", 3.0)]


def _sdmx_ml(version: str) -> str:
    if version == "2.1":
        m, s, c = ("http://www.sdmx.org/resources/sdmxml/schemas/v2_1/" + x for x in ("message", "structure", "common"))
        enum = '<s:LocalRepresentation><s:Enumeration><Ref id="CL_FREQ" agencyID="ECB"/></s:Enumeration></s:LocalRepresentation>'
        concept = '<s:ConceptIdentity><Ref id="FREQ" agencyID="ECB"/></s:ConceptIdentity>'
    else:
        m, s, c = ("http://www.sdmx.org/resources/sdmxml/schemas/v3_0/" + x for x in ("message", "structure", "common"))
        enum = "<s:LocalRepresentation><s:Enumeration>urn:sdmx:org.sdmx.infomodel.codelist.Codelist=ECB:CL_FREQ(1.0)</s:Enumeration></s:LocalRepresentation>"
        concept = "<s:ConceptIdentity>urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=ECB:CS(1.0).FREQ</s:ConceptIdentity>"
    return f"""<?xml version="1.0"?><m:Structure xmlns:m="{m}" xmlns:s="{s}" xmlns:c="{c}"><m:Structures>
      <s:Dataflows><s:Dataflow id="HICP" agencyID="ECB" version="1.0"><c:Name xml:lang="en">Indices of Consumer Prices</c:Name></s:Dataflow></s:Dataflows>
      <s:Codelists><s:Codelist id="CL_FREQ" agencyID="ECB" version="1.0"><s:Code id="M"><c:Name xml:lang="en">Monthly</c:Name></s:Code>
        <s:Code id="A"><c:Name xml:lang="en">Annual</c:Name></s:Code></s:Codelist></s:Codelists>
      <s:Concepts><s:ConceptScheme id="CS" agencyID="ECB"><s:Concept id="FREQ"><c:Name xml:lang="en">Frequency</c:Name></s:Concept></s:ConceptScheme></s:Concepts>
      <s:DataStructures><s:DataStructure id="ECB_ICP3" agencyID="ECB"><s:DataStructureComponents><s:DimensionList>
        <s:Dimension id="FREQ" position="1">{concept}{enum}</s:Dimension><s:TimeDimension id="TIME_PERIOD"/>
      </s:DimensionList><s:AttributeList><s:Attribute id="UNIT"/></s:AttributeList></s:DataStructureComponents></s:DataStructure></s:DataStructures>
    </m:Structures></m:Structure>"""


@pytest.mark.parametrize("version", ["2.1", "3.0"])
def test_sdmx_ml_structures_summarise_like_json(version):
    summary = summarize_structure_xml(_sdmx_ml(version), "ECB:HICP(1.0)", "u")
    assert summary["name"] == "Indices of Consumer Prices"
    assert summary["dimensions"] == [
        {"id": "FREQ", "position": 1, "name": "Frequency", "codelist": "ECB:CL_FREQ",
         "codes": [{"code": "M", "name": "Monthly"}, {"code": "A", "name": "Annual"}]}
    ]
    assert summary["attributes"] == [{"id": "UNIT", "name": None}]


async def test_metadata_and_search_for_new_institutions(fake_all):
    _, sdmx = fake_all
    sdmx.structures["HICP"] = _sdmx_ml("2.1")
    sdmx.wb.add("WB_WDI_SP_POP_TOTL", "USA", {"2025": 341784857}, unit="PS", name="Population, total")
    ecb = json.loads(await ok("get_metadata", {"source": "ECB", "dataflow": "ECB:HICP(1.0)"}))
    assert ecb["key_template"] == "{FREQ}" and ecb["dimensions"][0]["codes"][0]["code"] == "M"
    wb = json.loads(await ok("get_metadata", {"source": "World Bank", "dataflow": "WB_WDI_SP_POP_TOTL"}))
    assert (wb["provider"], wb["name"], wb["key_template"]) == ("WB", "Population, total", "{INDICATOR}.{REF_AREA}")
    found = json.loads(await ok("search_statistics", {"query": "population", "scope": "dataflows", "source": "WB"}))
    assert found["world_bank_indicators"][0]["indicator"] == "WB_WDI_SP_POP_TOTL"
    is_error, text = await call("get_metadata", {"source": "FRED", "dataflow": "X"})
    assert is_error and "one of OECD, IMF, BIS, ECB, EUROSTAT, WB" in text
