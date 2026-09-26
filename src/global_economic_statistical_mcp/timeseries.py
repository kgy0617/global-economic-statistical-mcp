"""Helpers for ECOS StatisticSearch rows (grouping, labels, numbers) and compact JSON."""

from __future__ import annotations

import json
import math
from typing import Any

TRANSFORMS = {
    "yoy": "yoy_pct",  # year-on-year change (%)
    "pop": "pop_pct",  # change on the previous observation (%)
}


def dumps(data: Any) -> str:
    """Serialize to JSON without whitespace (keeps Korean readable)."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def to_number(value: Any) -> float | int | str | None:
    """Parse ECOS DATA_VALUE strings into numbers where possible."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return value if math.isfinite(value) else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return str(value)
    if not math.isfinite(number):
        return None
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
