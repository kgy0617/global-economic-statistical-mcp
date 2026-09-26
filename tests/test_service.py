"""Concept/provider resolution, fallback and canonical transforms."""

import httpx
import pytest
from conftest import TEST_KEY, monthly

from global_economic_statistical_mcp.ecos_client import EcosClient
from global_economic_statistical_mcp.service import ResolutionError, StatService

pytestmark = pytest.mark.anyio


@pytest.fixture
async def service():
    s = StatService(ecos_client=EcosClient(api_key=TEST_KEY, retry_backoff=0))
    s.http.max_retries = 0
    yield s
    await s.close()


def window(src):
    return {"M": ("2026-01", "2026-03"), "Q": ("2026-Q1", "2026-Q2"), "D": ("2026-01-01", "2026-03-31")}[src.freq]


async def test_resolution_orders_sources_by_country(service):
    kr = service.resolve(indicator="CPI", country="KR")
    us = service.resolve(indicator="CPI", country="USA")
    assert [s.provider for s in kr] == ["ECOS", "IMF", "BIS", "OECD"]
    assert [s.provider for s in us] == ["IMF", "BIS", "OECD"]
    assert us[0].key == "USA.CPI._T.IX.M"
    assert service.resolve(indicator="USD_EXCHANGE_RATE", country="JP", source="BIS")[0].key == "M.JP.JPY.A"


async def test_euro_area_uses_each_providers_own_area_code(service):
    keys = {s.provider: s.key for s in service.resolve(indicator="USD_EXCHANGE_RATE", country="유로존")}
    assert keys == {
        "IMF": "G163.XDC_USD.PA_RT.M",
        "BIS": "M.XM.EUR.A",
        "OECD": "EA20.M.CC.XDC_USD._Z._Z._Z._Z.N",
        "ECB": "M.USD.EUR.SP00.A",
        "WB": "WB_WDI_PA_NUS_FCRF.EMU",
    }
    # A dataflow can override the provider default (OECD national accounts use "EA", not "EA20").
    assert service.resolve(indicator="GDP_REAL_GROWTH_QOQ", country="EA")[0].key.startswith("Q.Y.EA.S1.")
    # Sources known not to publish the euro area are skipped (OECD CPI stopped at the enlargement).
    assert [s.provider for s in service.resolve(indicator="CPI", country="EA")] == ["EUROSTAT", "ECB", "BIS"]
    # Eurostat's HICP uses the changing-composition euro area; its other dataflows EA21.
    cpi = {s.provider: s.key for s in service.resolve(indicator="CPI", country="EA")}
    assert (cpi["EUROSTAT"], cpi["ECB"]) == ("M.I25.TOTAL.EA", "M.U2.N.000000.4D0.INX")
    assert service.resolve(indicator="UNEMPLOYMENT_RATE", country="EA", source="Eurostat")[0].key == "M.NSA.TOTAL.PC_ACT.T.EA21"


@pytest.mark.parametrize(
    ("indicator", "country", "message"),
    [
        ("USD_EXCHANGE_RATE", "US", "No source for USD_EXCHANGE_RATE"),  # USD per USD
        ("UNEMPLOYMENT_RATE", "CN", "OECD does not publish this statistic for CN"),
    ],
)
async def test_unpublished_combinations_are_reported_not_fetched(service, indicator, country, message):
    with pytest.raises(ResolutionError, match=message):
        service.resolve(indicator=indicator, country=country)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"indicator": "지수", "country": "KR"}, "matches several concepts"),
        ({"indicator": "없는지표xyz", "country": "KR"}, "No concept matches"),
        ({"indicator": "CPI"}, "country is required"),
        ({"indicator": "CPI", "country": "ZZ"}, "Unknown country"),
        ({"indicator": "M2", "country": "US"}, "No source for M2"),
        ({"indicator": "CPI", "source": "FRED"}, "Unsupported source"),
        ({"indicator": "CPI", "stat_code": "901Y009"}, "cannot be combined"),
        ({"dataflow": "BIS:WS_CBPOL(1.0)", "key": "M.KR"}, "set source to one of OECD"),
        ({"source": "BIS", "dataflow": "BIS:WS_CBPOL(1.0)", "key": "M.KR"}, "cycle"),
        ({"source": "BIS", "dataflow": "BIS:WS_CBPOL(1.0)", "freq": "M"}, "need a key"),
        ({}, "indicator"),
    ],
)
async def test_resolution_errors_explain_what_to_do(service, kwargs, message):
    with pytest.raises(ResolutionError) as excinfo:
        service.resolve(**kwargs)
    assert message in str(excinfo.value)


async def test_falls_back_to_next_source(fake_all, service):
    _, sdmx = fake_all
    sdmx.forced.append(httpx.Response(503))  # IMF down
    sdmx.add("OECD", "DSD_PRICES@DF_PRICES_ALL",
             {"REF_AREA": "USA", "FREQ": "M", "METHODOLOGY": "N", "MEASURE": "CPI", "UNIT_MEASURE": "IX",
              "EXPENDITURE": "_T", "ADJUSTMENT": "N", "TRANSFORMATION": "_Z"},
             {"2026-01": 140.0}, attrs={"BASE_PER": "2015"})
    loaded = await service.load_first(service.resolve(indicator="CPI", country="US"), window)
    assert loaded.source.provider == "OECD"
    assert loaded.attempts[0]["provider"] == "IMF" and "HTTP_503" in loaded.attempts[0]["result"]
    assert loaded.reports[0].status in ("pass", "info")


async def test_yoy_fetches_base_periods_separately(fake_all, service):
    ecos, _ = fake_all
    ecos.add_series("901Y009", "M", monthly(2025, [100] * 12 + [103, 104, 105]))
    [src] = [s for s in service.resolve(indicator="CPI_YOY", country="KR") if s.provider == "ECOS"]
    loaded = await service.load(src, "2026-01", "2026-03")
    series = loaded.series[0]
    assert [(o.period, o.value, o.source_value) for o in series.observations] == [
        ("2026-01", 3.0, 103), ("2026-02", 4.0, 104), ("2026-03", 5.0, 105)
    ]
    assert series.unit == "PC_YOY" and loaded.reports[0].status == "pass"
    assert any("yoy" in t for t in series.provenance.transformations)


async def test_cross_check_compares_all_sources(fake_all, service):
    ecos, sdmx = fake_all
    ecos.add_series("722Y001", "M", monthly(2026, [2.5, 2.5, 2.75]), item_code1="0101000", item_name1="기준금리", unit="연%")
    sdmx.add("BIS", "WS_CBPOL", {"FREQ": "M", "REF_AREA": "KR"}, {"2026-01": 2.5, "2026-02": 2.5, "2026-03": 2.75})
    from global_economic_statistical_mcp.catalog.concepts import get_concept
    from global_economic_statistical_mcp.catalog.countries import get_country

    result = await service.cross_check(get_concept("POLICY_RATE"), get_country("KR"), window)
    assert result["status"] == "consistent"
    assert [s["provider"] for s in result["sources"]] == ["ECOS", "BIS"]
    assert all(s["freq"] == "M" for s in result["sources"])  # monthly chosen over daily
    assert len(service.ledger.records()) >= 3


async def test_rate_limited_provider_falls_back(fake_all, service):
    _, sdmx = fake_all
    sdmx.forced.append(httpx.Response(429, text="quota"))  # IMF over quota
    sdmx.add("OECD", "DSD_PRICES@DF_PRICES_ALL",
             {"REF_AREA": "USA", "FREQ": "M", "METHODOLOGY": "N", "MEASURE": "CPI", "UNIT_MEASURE": "IX",
              "EXPENDITURE": "_T", "ADJUSTMENT": "N", "TRANSFORMATION": "_Z"},
             {"2026-01": 140.0}, attrs={"BASE_PER": "2015"})
    loaded = await service.load_first(service.resolve(indicator="CPI", country="US"), window)
    assert loaded.source.provider == "OECD"
    assert "RATE_LIMITED" in loaded.attempts[0]["result"]
