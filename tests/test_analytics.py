import pytest

from global_economic_statistical_mcp.analytics import (
    align,
    can_convert,
    coarsest_cycle,
    convert_frequency,
    correlation_matrix,
    describe,
    normalize,
    years_between,
)


def test_years_between():
    assert years_between("M", "202401", "202501") == 1
    assert years_between("Q", "2024Q1", "2026Q1") == 2
    assert years_between("D", "20240101", "20250101") == pytest.approx(366 / 365.25)


def test_describe_rates_use_percentage_points_and_short_spans_have_no_cagr():
    stats = describe("M", [("202511", 2.45), ("202601", 2.0), ("202608", 3.09)], is_rate=True)
    assert stats["change_pp"] == 0.64
    assert "change_pct" not in stats and "cagr_pct" not in stats and "max_drawdown_pct" not in stats
    short = describe("D", [("20260910", 3.93), ("20260923", 4.006)])
    assert "cagr_pct" not in short
    negative = describe("M", [("202601", -5.0), ("202602", 10.0)])
    assert negative["change_pct"] is None and "max_drawdown_pct" not in negative


def test_describe_basic_statistics():
    points = [("2020", 100.0), ("2021", 110.0), ("2022", 121.0)]
    stats = describe("A", points)
    assert stats["count"] == 3
    assert stats["min"] == {"time": "2020", "value": 100.0}
    assert stats["change_pct"] == 21.0
    assert stats["cagr_pct"] == 10.0
    assert stats["latest_pop_pct"] == 10.0
    assert stats["max_drawdown_pct"] == 0


def test_describe_drawdown_and_empty():
    stats = describe("M", [("202401", 100.0), ("202402", 80.0), ("202403", 90.0)])
    assert stats["max_drawdown_pct"] == -20.0
    assert describe("M", []) == {"count": 0}


def test_frequency_rules():
    assert coarsest_cycle(["D", "M", "Q"]) == "Q"
    assert can_convert("D", "M") and not can_convert("Q", "M")


def test_convert_frequency():
    daily = [("20240101", 1.0), ("20240131", 3.0), ("20240201", 5.0)]
    assert convert_frequency(daily, "D", "M", "mean") == [("202401", 2.0), ("202402", 5.0)]
    assert convert_frequency(daily, "D", "M", "last") == [("202401", 3.0), ("202402", 5.0)]
    monthly = [("202401", 1.0), ("202402", 2.0), ("202403", 3.0), ("202404", 4.0)]
    assert convert_frequency(monthly, "M", "Q", "sum") == [("2024Q1", 6.0), ("2024Q2", 4.0)]


def test_align_and_correlation():
    a = [("1", 1.0), ("2", 2.0), ("3", 3.0), ("4", 4.0)]
    b = [("2", 20.0), ("3", 10.0), ("4", 0.0), ("5", 5.0)]
    assert align([a, b], "inner") == [["2", 2.0, 20.0], ["3", 3.0, 10.0], ["4", 4.0, 0.0]]
    assert align([a, b], "outer")[0] == ["1", 1.0, None]
    corr = correlation_matrix(["a", "b"], align([a, b], "outer"))
    assert corr == {"a ~ b": {"r": -1.0, "n": 3}}


def test_normalize():
    assert normalize([("1", 50.0), ("2", 75.0)], "index") == [("1", 100.0), ("2", 150.0)]
    z = normalize([("1", 1.0), ("2", 3.0)], "zscore")
    assert [v for _, v in z] == pytest.approx([-0.7071, 0.7071], abs=1e-4)


def test_scale_multiplier():
    from global_economic_statistical_mcp.analytics import scale_multiplier

    points = [("202401", 5.0), ("202402", 10.0)]
    # Scale from 십억원 (9) to 백만원 (6): 5 * 10^3 = 5000
    scaled = scale_multiplier(points, 9, 6)
    assert scaled == [("202401", 5000.0), ("202402", 10000.0)]

    # Scale from 원 (0) to 십억원 (9)
    scaled_down = scale_multiplier([("202401", 1_000_000_000.0)], 0, 9)
    assert scaled_down == [("202401", 1.0)]


def test_rebase_index():
    from global_economic_statistical_mcp.analytics import normalize, rebase_index

    series = [("202301", 100.0), ("202401", 120.0), ("202402", 132.0)]
    rebased = rebase_index(series, base_period="202401", base_value=100.0)
    assert rebased[0][1] == pytest.approx(100.0 / 120.0 * 100.0, abs=1e-2)
    assert rebased[1][1] == 100.0
    assert rebased[2][1] == 110.0

    # Also via normalize(..., "rebase")
    norm_rebased = normalize(series, "rebase", base_period="202401")
    assert norm_rebased[1][1] == 100.0


def test_convert_currency():
    from global_economic_statistical_mcp.analytics import convert_currency

    usd_series = [("202401", 10.0), ("202402", 20.0)]
    fx_rates = [("202401", 1300.0), ("202402", 1400.0)]
    krw_series = convert_currency(usd_series, fx_rates, direction="usd_to_krw")
    assert krw_series == [("202401", 13000.0), ("202402", 28000.0)]

