from datetime import date

import pytest

from ecos_mcp.config import (
    POPULAR_INDICATORS,
    VALID_CYCLES,
    find_popular_indicator,
    get_default_date_range,
    load_api_key,
    match_popular_indicators,
    parse_any_period,
    shift_period,
    to_cycle,
    validate_date_format,
)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_api_key_falls_back_to_sample(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ECOS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ECOS_API_KEY", value)
    assert load_api_key() == "sample"


def test_api_key_is_stripped(monkeypatch):
    monkeypatch.setenv("ECOS_API_KEY", "  abc123 ")
    assert load_api_key() == "abc123"


@pytest.mark.parametrize(
    ("cycle", "value", "ok"),
    [
        ("A", "2024", True),
        ("S", "2024S2", True),
        ("S", "2024S3", False),
        ("Q", "2024Q1", True),
        ("Q", "20241", False),
        ("M", "202413", False),
        ("SM", "202401S1", True),
        ("D", "20240229", True),
        ("D", "20230229", False),
        ("X", "2024", False),
    ],
)
def test_validate_date_format(cycle, value, ok):
    assert validate_date_format(cycle, value)[0] is ok


@pytest.mark.parametrize(
    ("cycle", "value", "periods", "expected"),
    [
        ("A", "2024", -2, "2022"),
        ("S", "2024S1", -1, "2023S2"),
        ("Q", "2024Q1", -4, "2023Q1"),
        ("M", "202401", -1, "202312"),
        ("SM", "202401S1", -1, "202312S2"),
        ("D", "20240301", -1, "20240229"),
    ],
)
def test_shift_period(cycle, value, periods, expected):
    assert shift_period(cycle, value, periods) == expected


def test_default_date_range_per_cycle():
    today = date(2026, 9, 26)
    assert get_default_date_range("M", today=today) == ("202409", "202609")
    assert get_default_date_range("Q", today=today) == ("2024Q3", "2026Q3")
    assert get_default_date_range("A", recent_years=5, today=today) == ("2021", "2026")
    # Daily series default to a short window to keep responses small.
    assert get_default_date_range("D", today=today) == ("20260628", "20260926")


def test_default_date_range_handles_leap_day():
    assert get_default_date_range("D", recent_years=1, today=date(2028, 2, 29)) == (
        "20270228",
        "20280229",
    )


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("기준금리", "base_rate"),
        ("금리", "base_rate"),
        ("최근 기준금리 추이", "base_rate"),
        ("성장률", "gdp_growth"),
        ("경제성장률", "gdp_growth"),
        ("GDP", "gdp"),
        ("물가상승률", "inflation"),
        ("최근 소비자물가 상승률", "inflation"),
        ("물가", "cpi"),
        ("CPI", "cpi"),
        ("원/달러 환율", "usd_krw"),
        ("월평균환율", "usd_krw_monthly"),
        ("국고채 3년 월평균", "treasury_3y_monthly"),
        ("PPI", "ppi"),
    ],
)
def test_find_popular_indicator(keyword, expected):
    preset = find_popular_indicator(keyword)
    assert preset is not None and preset["id"] == expected


@pytest.mark.parametrize("keyword", ["", "   ", "통화", "지수", "존재하지않는지표"])
def test_find_popular_indicator_rejects_empty_and_ambiguous(keyword):
    assert find_popular_indicator(keyword) is None


def test_ambiguous_keyword_lists_candidates():
    ids = {p["id"] for p in match_popular_indicators("통화")}
    assert ids == {"reserve_money", "m2"}


def test_presets_are_well_formed():
    ids = [p["id"] for p in POPULAR_INDICATORS]
    assert len(ids) == len(set(ids))
    for preset in POPULAR_INDICATORS:
        assert preset["cycle"] in VALID_CYCLES
        assert preset["stat_code"] and preset["item_code1"]
        assert preset.get("transform") in (None, "yoy", "pop")
        if preset.get("transform") == "yoy":
            assert preset["cycle"] != "D"


@pytest.mark.parametrize(
    ("value", "cycle", "bound", "expected"),
    [
        ("2024", "M", "start", "202401"),
        ("2024", "M", "end", "202412"),
        ("2024-03", "Q", "end", "2024Q1"),
        ("2024Q2", "M", "end", "202406"),
        ("2024-01-15", "M", "start", "202401"),
        ("20240115", "SM", "start", "202401S1"),
        ("202402", "D", "end", "20240229"),
        ("202401", "M", "start", "202401"),
    ],
)
def test_to_cycle(value, cycle, bound, expected):
    assert to_cycle(value, cycle, bound) == expected


@pytest.mark.parametrize("value", ["2024-13", "24", "2024Q5", "abc"])
def test_parse_any_period_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_any_period(value)


def test_today_is_korean_date():
    from datetime import datetime

    from ecos_mcp.config import KST, today_kst

    assert today_kst() == datetime.now(KST).date()
