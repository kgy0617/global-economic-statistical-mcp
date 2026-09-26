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
    assert [s.provider for s in kr] == ["ECOS", "IMF", "OECD"]
    assert [s.provider for s in us] == ["IMF", "OECD"]
    assert us[0].key == "USA.CPI._T.IX.M"
    assert service.resolve(indicator="USD_EXCHANGE_RATE", country="JP", source="BIS")[0].key == "M.JP.JPY.A"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"indicator": "지수"}, "여러 개"),
        ({"indicator": "없는지표xyz"}, "표준 개념이 없습니다"),
        ({"indicator": "CPI", "country": "ZZ"}, "알 수 없는 국가"),
        ({"indicator": "M2", "country": "US"}, "출처가 없습니다"),
        ({"indicator": "CPI", "source": "FRED"}, "지원하지 않는 source"),
        ({"indicator": "CPI", "stat_code": "901Y009"}, "함께 쓸 수 없습니다"),
        ({"dataflow": "BIS:WS_CBPOL(1.0)", "key": "M.KR"}, "source를 OECD"),
        ({"source": "BIS", "dataflow": "BIS:WS_CBPOL(1.0)", "key": "M.KR"}, "cycle"),
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
