import logging

import httpx
import pytest
from conftest import TEST_KEY, monthly

from global_economic_statistical_mcp.ecos_client import EcosApiError, EcosClient

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client():
    c = EcosClient(api_key=TEST_KEY, retry_backoff=0)
    yield c
    await c.close()


@pytest.fixture
async def sample_client():
    c = EcosClient(api_key="sample", retry_backoff=0)
    yield c
    await c.close()


def test_httpx_request_logging_is_silenced():
    # httpx logs full request URLs (which contain the API key) at INFO.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


async def test_url_segments_are_encoded(client):
    url = client._build_url("StatisticSearch", "k", "404Y014", "*AA", "원/달러")
    assert url.endswith("/404Y014/%2AAA/%EC%9B%90%20%EB%8B%AC%EB%9F%AC")


async def test_http_error_does_not_leak_api_key(fake_ecos, client):
    fake_ecos.forced.append(httpx.Response(404))
    with pytest.raises(EcosApiError) as excinfo:
        await client.get_key_statistics()
    assert excinfo.value.code == "HTTP_404"
    assert TEST_KEY not in str(excinfo.value)


async def test_network_error_does_not_leak_api_key(fake_ecos, client):
    request = httpx.Request("GET", f"https://ecos.bok.or.kr/api/X/{TEST_KEY}/json")
    fake_ecos.forced.extend([httpx.ConnectError(f"failed {request.url}", request=request)] * 3)
    with pytest.raises(EcosApiError) as excinfo:
        await client.get_key_statistics()
    assert excinfo.value.code == "NETWORK_ERROR"
    assert TEST_KEY not in str(excinfo.value)


async def test_retries_transient_failures(fake_ecos, client):
    fake_ecos.lists["KeyStatisticList"] = [{"KEYSTAT_NAME": "기준금리"}]
    fake_ecos.forced.extend([httpx.Response(503), httpx.ReadTimeout("slow")])
    res = await client.get_key_statistics()
    assert res["count"] == 1


async def test_gives_up_after_max_retries(fake_ecos, client):
    fake_ecos.forced.extend([httpx.Response(503)] * 3)
    with pytest.raises(EcosApiError) as excinfo:
        await client.get_key_statistics()
    assert excinfo.value.code == "HTTP_503"


async def test_client_errors_are_not_retried(fake_ecos, client):
    fake_ecos.forced.extend([httpx.Response(400), httpx.Response(200, json={})])
    with pytest.raises(EcosApiError):
        await client.get_key_statistics()
    assert len(fake_ecos.forced) == 1


async def test_no_data_is_an_empty_result(fake_ecos, client):
    res = await client.search_statistics("722Y001", "D", "19000101", "19000105")
    assert res["rows"] == [] and res["total_count"] == 0
    assert "데이터가 없습니다" in res["note"]


async def test_nested_error_raises(fake_ecos, client):
    fake_ecos.forced.append(
        httpx.Response(
            200,
            json={"StatisticSearch": {"RESULT": {"CODE": "ERROR-101", "MESSAGE": "주기 오류"}}},
        )
    )
    with pytest.raises(EcosApiError) as excinfo:
        await client.search_statistics("901Y009", "M", "2024", "2025")
    assert excinfo.value.code == "ERROR-101"


async def test_sample_key_is_clamped_and_noted_only_when_cut(fake_ecos, sample_client):
    fake_ecos.lists["KeyStatisticList"] = [{"KEYSTAT_NAME": str(i)} for i in range(25)]
    res = await sample_client.get_key_statistics(end_count=100)
    assert fake_ecos.calls[-1][4:6] == ["1", "10"]
    assert res["count"] == 10 and res["has_more"] and "sample" in res["note"]

    fake_ecos.lists["KeyStatisticList"] = [{"KEYSTAT_NAME": "1"}]
    res = await sample_client.get_key_statistics(end_count=100)
    assert "note" not in res


async def test_truncated_search_returns_latest_rows(fake_ecos, client):
    fake_ecos.add_series("901Y009", "M", monthly(2024, list(range(1, 25))))
    res = await client.search_statistics("901Y009", "M", "202401", "202412", end_count=5)
    assert [r["TIME"] for r in res["rows"]] == ["202408", "202409", "202410", "202411", "202412"]
    assert res["truncated"] and "최근 5건" in res["note"]

    res = await client.search_statistics(
        "901Y009", "M", "202401", "202412", end_count=5, prefer_latest=False
    )
    assert res["rows"][0]["TIME"] == "202401" and res["truncated"]


async def test_metadata_responses_are_cached(fake_ecos, client):
    fake_ecos.lists["StatisticItemList"] = [{"ITEM_CODE": "0"}]
    await client.list_statistic_items("901Y009")
    await client.list_statistic_items("901Y009")
    assert len(fake_ecos.calls) == 1


async def test_local_search_normalizes_spaces_and_ranks(client):
    res = client.search_statistic_tables("소비자 물가", limit=10)
    assert res["rows"][0]["STAT_CODE"] == "901Y009"
    assert res["index_generated_at"]


async def test_local_search_reports_true_total(client):
    res = client.search_statistic_tables("금리", limit=2)
    assert res["count"] == 2
    assert res["total_matches"] > 2


async def test_local_search_within_parent(client):
    res = client.search_statistic_tables("지수", parent_code="0000000202", limit=100)
    codes = {r["STAT_CODE"] for r in res["rows"]}
    assert "901Y009" in codes
    assert "513Y001" not in codes  # 경제심리지수 lives under 6. 심리지수


async def test_browse_root_and_children(client):
    root = client.browse_statistic_tables()
    assert root["count"] >= 5 and all(r["P_STAT_CODE"] == "*" for r in root["rows"])
    first = root["rows"][0]["STAT_CODE"]
    children = client.browse_statistic_tables(first)
    assert children["parent"]["STAT_CODE"] == first and children["count"] > 0
