"""Descriptive statistics, frequency conversion and alignment for ECOS time series."""

from __future__ import annotations

import math
import statistics
from itertools import pairwise
from typing import Any

from global_economic_statistical_mcp.config import (
    PERIODS_PER_YEAR,
    convert_period,
    period_start_date,
    period_to_index,
)
from global_economic_statistical_mcp.ecos_sdmx import FREQ_ORDER

AGGREGATIONS = {"mean", "last", "first", "sum"}
NORMALIZATIONS = {"none", "index", "zscore", "rebase"}

Point = tuple[str, float]


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def years_between(cycle: str, start: str, end: str) -> float:
    """Elapsed years between two periods of the same cycle."""
    if cycle == "D":
        return (period_start_date("D", end) - period_start_date("D", start)).days / 365.25
    if cycle == "SM":
        return (period_to_index(cycle, end) - period_to_index(cycle, start)) / 24
    return (period_to_index(cycle, end) - period_to_index(cycle, start)) / PERIODS_PER_YEAR[cycle]


def _linear_trend(cycle: str, points: list[Point]) -> tuple[float | None, float | None]:
    """OLS slope in value units per year, and R²."""
    if len(points) < 3:
        return None, None
    xs = [years_between(cycle, points[0][0], t) for t, _ in points]
    ys = [v for _, v in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (mean_y + slope * (x - mean_x))) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else None
    return slope, r2


def _max_drawdown_pct(values: list[float]) -> float | None:
    peak = None
    worst = 0.0
    for v in values:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            worst = min(worst, (v / peak - 1) * 100)
    return worst if peak and peak > 0 else None


def describe(
    cycle: str, points: list[Point], yoy: list[Point] | None = None, *, is_rate: bool = False
) -> dict[str, Any]:
    """Summary statistics for one series of (time, value) points in time order.

    For rates and percentages (``is_rate``: interest rates, inflation, growth, unemployment)
    changes are reported in percentage points; percent changes of a percentage would mislead
    (2.45% → 3.09% is +0.64pp, not "+26%"). CAGR is only given for spans of at least a year
    with positive endpoints, and percent changes only from a positive base.
    """
    points = [(t, v) for t, v in points if isinstance(v, int | float)]
    if not points:
        return {"count": 0}
    values = [v for _, v in points]
    first, last = points[0], points[-1]
    low = min(points, key=lambda p: p[1])
    high = max(points, key=lambda p: p[1])

    out: dict[str, Any] = {
        "count": len(points),
        "first": {"time": first[0], "value": first[1]},
        "last": {"time": last[0], "value": last[1]},
        "min": {"time": low[0], "value": low[1]},
        "max": {"time": high[0], "value": high[1]},
        "mean": _round(statistics.fmean(values)),
        "median": _round(statistics.median(values)),
        "std": _round(statistics.stdev(values)) if len(values) > 1 else None,
    }

    slope, r2 = _linear_trend(cycle, points)
    diffs = [b - a for (_, a), (_, b) in pairwise(points)]

    if is_rate:
        out["change_pp"] = _round(last[1] - first[1])
        if diffs:
            out["latest_change_pp"] = _round(diffs[-1])
        if len(diffs) > 1:
            out["change_pp_std"] = _round(statistics.stdev(diffs), 4)
        if slope is not None:
            out["trend_pp_per_year"] = _round(slope)
            out["trend_r2"] = _round(r2, 3)
        return out

    out["change"] = _round(last[1] - first[1])
    out["change_pct"] = _round((last[1] / first[1] - 1) * 100, 2) if first[1] > 0 else None
    years = years_between(cycle, first[0], last[0])
    if years >= 1 and first[1] > 0 and last[1] > 0:
        out["cagr_pct"] = _round(((last[1] / first[1]) ** (1 / years) - 1) * 100, 2)
    if slope is not None:
        out["trend_per_year"] = _round(slope)
        out["trend_r2"] = _round(r2, 3)

    changes = [(b / a - 1) * 100 for (_, a), (_, b) in pairwise(points) if a > 0]
    if len(changes) > 1:
        out["pop_pct_std"] = _round(statistics.stdev(changes), 3)
    if changes:
        out["latest_pop_pct"] = _round(changes[-1], 2)

    if all(v > 0 for v in values):
        drawdown = _max_drawdown_pct(values)
        if drawdown is not None:
            out["max_drawdown_pct"] = _round(drawdown, 2)

    yoy_values = [(t, v) for t, v in (yoy or []) if isinstance(v, int | float)]
    if yoy_values:
        out["latest_yoy_pct"] = yoy_values[-1][1]
        out["mean_yoy_pct"] = _round(statistics.fmean(v for _, v in yoy_values), 2)
    return out


# ── Frequency conversion & alignment ───────────────────────────────


def coarsest_cycle(cycles: list[str]) -> str:
    return max(cycles, key=FREQ_ORDER.index)


def can_convert(from_cycle: str, to_cycle: str) -> bool:
    """Only aggregation to an equal or lower frequency is supported."""
    return FREQ_ORDER.index(to_cycle) >= FREQ_ORDER.index(from_cycle)


def convert_frequency(
    points: list[Point], from_cycle: str, to_cycle: str, how: str = "mean", complete_only: bool = False
) -> list[Point]:
    """Aggregate (time, value) points to a lower frequency.

    With ``complete_only`` a target period is kept only when every sub-period is present
    (3 months for a quarter, 4 quarters for a year): the sum of two quarters is not a year.
    Daily data has no fixed count and is never dropped.
    """
    if from_cycle == to_cycle:
        return points
    buckets: dict[str, list[float]] = {}
    for time, value in points:
        buckets.setdefault(convert_period(time, from_cycle, to_cycle), []).append(value)
    expected = (
        PERIODS_PER_YEAR[from_cycle] // PERIODS_PER_YEAR[to_cycle]
        if complete_only and from_cycle in PERIODS_PER_YEAR and to_cycle in PERIODS_PER_YEAR
        else 0
    )
    out = []
    for period in sorted(buckets):
        values = buckets[period]
        if len(values) < expected:
            continue
        if how == "last":
            agg = values[-1]
        elif how == "first":
            agg = values[0]
        elif how == "sum":
            agg = sum(values)
        else:
            agg = statistics.fmean(values)
        out.append((period, _round(agg, 6)))
    return out


# Multiplier mapping for currency and quantity units
def scale_multiplier(points: list[Point], from_mult: int, to_mult: int) -> list[Point]:
    """Scale observation values between decimal multipliers.

    Example:
        from_mult=9 (billions) to to_mult=0 (units) multiplies by 10^9.
        from_mult=0 (units) to to_mult=9 (billions) multiplies by 10^-9.
    """
    if from_mult == to_mult or not points:
        return points
    factor = 10 ** (from_mult - to_mult)
    return [(t, _round(v * factor, 6) if v is not None else None) for t, v in points]


def rebase_index(
    points: list[Point], base_period: str | None = None, base_value: float = 100.0
) -> list[Point]:
    """Rebase an index series so that the value at base_period equals base_value.

    If base_period is None or not present in the series, the first available observation
    is used as the reference base.
    """
    valid = [(t, v) for t, v in points if isinstance(v, int | float) and v != 0]
    if not valid:
        return points

    base_obs = None
    if base_period:
        for t, v in valid:
            if t == base_period or t.startswith(base_period):
                base_obs = v
                break

    if base_obs is None:
        base_obs = valid[0][1]

    if base_obs == 0:
        return points

    return [(t, _round((v / base_obs) * base_value, 4) if v is not None else None) for t, v in points]


def normalize(
    points: list[Point],
    how: str,
    base_period: str | None = None,
    base_value: float = 100.0,
) -> list[Point]:
    if how == "rebase":
        return rebase_index(points, base_period=base_period, base_value=base_value)
    if how == "index" and points and points[0][1]:
        base = points[0][1]
        return [(t, _round(v / base * 100, 4)) for t, v in points]
    if how == "zscore" and len(points) > 1:
        values = [v for _, v in points if v is not None]
        mean, std = statistics.fmean(values), statistics.stdev(values)
        if std:
            return [(t, _round((v - mean) / std, 4)) for t, v in points]
    return points


def align(series: list[list[Point]], how: str = "inner") -> list[list[Any]]:
    """Join series on time: rows of [time, v1, v2, ...]; inner keeps common periods only."""
    maps = [dict(points) for points in series]
    if how == "inner":
        times = set(maps[0]) if maps else set()
        for m in maps[1:]:
            times &= set(m)
    else:
        times = set().union(*maps) if maps else set()
    return [[t, *(m.get(t) for m in maps)] for t in sorted(times)]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return _round(statistics.correlation(xs, ys), 4)
    except statistics.StatisticsError:  # constant input
        return None


def correlation_matrix(labels: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """Pairwise Pearson correlations over periods where both series have values."""
    out: dict[str, Any] = {}
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            pairs = [
                (r[i + 1], r[j + 1])
                for r in rows
                if isinstance(r[i + 1], int | float) and isinstance(r[j + 1], int | float)
            ]
            out[f"{labels[i]} ~ {labels[j]}"] = {
                "r": pearson([a for a, _ in pairs], [b for _, b in pairs]),
                "n": len(pairs),
            }
    return out
