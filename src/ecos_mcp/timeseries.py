"""Time-series post-processing and token-efficient formatting for StatisticSearch rows."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from ecos_mcp.config import PERIODS_PER_YEAR, period_to_index

TRANSFORMS = {
    "yoy": "yoy_pct",  # 전년동기대비 증감률(%)
    "pop": "pop_pct",  # 직전 관측치 대비 증감률(%)
}
OUTPUT_FORMATS = {"compact", "csv", "json"}


def dumps(data: Any) -> str:
    """Serialize to JSON without whitespace (keeps Korean readable)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def to_number(value: Any) -> float | int | str | None:
    """Parse ECOS DATA_VALUE strings into numbers where possible."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return value
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return str(value)
    if number.is_integer() and "." not in text and "e" not in text.lower():
        return int(number)
    return number


def series_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(row.get(f"ITEM_CODE{i}") or "" for i in range(1, 5))


def series_label(row: dict[str, Any]) -> str:
    names = [row.get(f"ITEM_NAME{i}") for i in range(1, 5)]
    return " / ".join(n for n in names if n) or row.get("STAT_NAME", "")


def group_series(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group rows by item-code combination, preserving first-seen order."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(series_key(row), []).append(row)
    return list(groups.values())


def transform_lookback(cycle: str, transform: str | None) -> int:
    """Number of extra periods to fetch before start_date so the first row has a base."""
    if transform == "yoy":
        return PERIODS_PER_YEAR[cycle]
    if transform == "pop":
        return 10 if cycle == "D" else 1  # daily data skips weekends/holidays
    return 0


def _pct_change(current: Any, base: Any) -> float | None:
    if not isinstance(current, int | float) or not isinstance(base, int | float) or base == 0:
        return None
    return round((current / base - 1) * 100, 2)


def apply_transform(rows: list[dict[str, Any]], cycle: str, transform: str) -> None:
    """Add a percent-change field (yoy_pct / pop_pct) to each row, in place."""
    field = TRANSFORMS[transform]
    for series in group_series(rows):
        if transform == "yoy":
            lag = PERIODS_PER_YEAR[cycle]
            by_index = {period_to_index(cycle, r["TIME"]): r for r in series}
            for index, row in by_index.items():
                base = by_index.get(index - lag)
                row[field] = _pct_change(
                    to_number(row.get("DATA_VALUE")),
                    to_number(base.get("DATA_VALUE")) if base else None,
                )
        else:
            previous = None
            for row in series:
                row[field] = _pct_change(
                    to_number(row.get("DATA_VALUE")),
                    to_number(previous.get("DATA_VALUE")) if previous else None,
                )
                previous = row


def drop_unchanged(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only rows whose value differs from the previous row of the same series.

    The first and last row of every series are always kept, so the output still
    shows the full period covered.
    """
    keep: set[int] = set()
    for series in group_series(rows):
        previous_value = object()
        for position, row in enumerate(series):
            value = row.get("DATA_VALUE")
            if position in (0, len(series) - 1) or value != previous_value:
                keep.add(id(row))
            previous_value = value
    return [r for r in rows if id(r) in keep]


def format_timeseries(
    result: dict[str, Any],
    output_format: str = "compact",
    transform: str | None = None,
    meta: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> str:
    """Render StatisticSearch results as compact JSON, CSV, or raw JSON.

    compact: rows grouped per series as [time, value(, pct)] tuples with name/unit
             stated once per series.
    csv:     one row per observation; summary lines prefixed with '#'.
    json:    ECOS rows as returned (plus any transform field).
    """
    rows: list[dict[str, Any]] = result.get("rows", [])
    field = TRANSFORMS.get(transform or "")
    summary: dict[str, Any] = {
        "stat_code": rows[0].get("STAT_CODE") if rows else None,
        "stat_name": rows[0].get("STAT_NAME") if rows else None,
        **(meta or {}),
        "total_count": result.get("total_count", len(rows)),
        "count": len(rows),
    }
    if evidence:
        summary["evidence"] = evidence
    for key in ("truncated", "note"):
        if result.get(key):
            summary[key] = result[key]
    summary = {k: v for k, v in summary.items() if v is not None}

    if output_format == "json":
        return dumps({**summary, "data": rows})

    if output_format == "csv":
        buffer = io.StringIO()
        if evidence:
            buffer.write(f"# evidence_agency: {evidence.get('source_agency', '')}\n")
            buffer.write(f"# evidence_table: {evidence.get('table_name', '')} ({evidence.get('table_code', '')})\n")
            buffer.write(f"# evidence_series_key: {evidence.get('series_key', '')}\n")
            buffer.write(f"# evidence_period: {evidence.get('period', '')}\n")
            buffer.write(f"# evidence_retrieved_at: {evidence.get('retrieved_at', '')}\n")
            buffer.write(f"# evidence_url: {evidence.get('ecos_url', '')}\n")
            buffer.write(f"# evidence_citation: {evidence.get('citation', '')}\n")
        for key, value in summary.items():
            if key == "evidence":
                continue
            buffer.write(f"# {key}: {value}\n")
        writer = csv.writer(buffer, lineterminator="\n")
        header = ["TIME", "ITEM_CODE", "ITEM", "VALUE", "UNIT"]
        if field:
            header.append(field.upper())
        writer.writerow(header)
        for r in rows:
            line = [
                r.get("TIME", ""),
                "/".join(c for c in series_key(r) if c),
                series_label(r),
                r.get("DATA_VALUE", ""),
                r.get("UNIT_NAME", ""),
            ]
            if field:
                line.append("" if r.get(field) is None else r[field])
            writer.writerow(line)
        return buffer.getvalue().rstrip("\n")

    columns = ["time", "value"] + ([field] if field else [])
    series_out = []
    for series in group_series(rows):
        first = series[0]
        data = []
        for r in series:
            point = [r.get("TIME", ""), to_number(r.get("DATA_VALUE"))]
            if field:
                point.append(r.get(field))
            data.append(point)
        series_out.append(
            {
                "item": series_label(first),
                "item_code": "/".join(c for c in series_key(first) if c),
                "unit": first.get("UNIT_NAME", ""),
                "data": data,
            }
        )
    return dumps({**summary, "columns": columns, "series": series_out})
