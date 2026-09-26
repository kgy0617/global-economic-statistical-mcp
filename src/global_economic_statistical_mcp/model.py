"""Canonical time-series model shared by every provider.

Every adapter (ECOS, OECD, IMF, BIS) converts its native response into
:class:`CanonicalSeries`, so validation, analysis and formatting never need to know
where a series came from.

Canonical conventions
---------------------
- Frequency: ``A`` (annual), ``S`` (half-year), ``Q`` (quarter), ``M`` (month),
  ``SM`` (half-month, ECOS only), ``D`` (day).
- Period: SDMX reporting-period style — ``2026``, ``2026-S1``, ``2026-Q1``, ``2026-01``,
  ``2026-01-S1`` (half-month), ``2026-01-15``.
- Country: ISO 3166-1 alpha-2 (``KR``, ``US``), ``EA`` for the euro area.
- Unit: a small SDMX-flavoured vocabulary (see :data:`UNIT_LABELS`) plus ``unit_mult``
  (power of ten) and ``base_period`` for index numbers.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from global_economic_statistical_mcp.config import KST, period_to_index

FREQUENCIES = ("D", "SM", "M", "Q", "S", "A")  # highest to lowest frequency

UNIT_LABELS: dict[str, str] = {
    "PC_PA": "percent per annum",
    "PC": "percent",
    "PC_YOY": "percent change, year on year",
    "PC_POP": "percent change, period on period",
    "IX": "index",
    "PB": "percentage balance",
    "XDC_USD": "national currency per US dollar",
    "XDC": "national currency",
    "KRW": "Korean won",
    "USD": "US dollar",
    "PS": "persons",
}

# Units that describe the same quantity under different provider codes.
UNIT_EQUIVALENTS: dict[str, str] = {
    "PA": "PC_PA",
    "PC_PA": "PC_PA",
    "PT_LF_SUB": "PC",
    "PC": "PC",
    "GY": "PC_YOY",
    "PC_YOY": "PC_YOY",
    "G1": "PC_POP",
    "PC_POP": "PC_POP",
    "IX": "IX",
    "XDC_USD": "XDC_USD",
    "KRW": "XDC",
    "XDC": "XDC",
    "USD": "USD",
}


def canonical_unit(code: str | None) -> str | None:
    if code is None:
        return None
    return UNIT_EQUIVALENTS.get(code.strip().upper(), code.strip().upper())


# ── Periods ─────────────────────────────────────────────────────────

_PERIOD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("D", re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")),
    ("SM", re.compile(r"^(\d{4})-?(\d{2})-?S([12])$")),
    ("M", re.compile(r"^(\d{4})-?M?(\d{2})$")),
    ("Q", re.compile(r"^(\d{4})-?Q([1-4])$")),
    ("S", re.compile(r"^(\d{4})-?[SH]([12])$")),
    ("A", re.compile(r"^(\d{4})(?:-A1?)?$")),
]


def parse_period(value: str, freq: str | None = None) -> tuple[str, str]:
    """Parse any provider period string into (freq, canonical period).

    Accepts ECOS (``202601``, ``2026Q1``, ``20260115``), SDMX (``2026-01``, ``2026-Q1``),
    IMF (``2026-M01``) and ISO date forms. ``freq`` disambiguates when given.
    """
    text = str(value).strip().upper()
    candidates = [(f, p) for f, p in _PERIOD_PATTERNS if freq is None or f == freq]
    for f, pattern in candidates:
        match = pattern.match(text)
        if not match:
            continue
        g = match.groups()
        if f == "D":
            try:
                date(int(g[0]), int(g[1]), int(g[2]))
            except ValueError:
                continue
            return f, f"{g[0]}-{g[1]}-{g[2]}"
        if f == "SM":
            return f, f"{g[0]}-{g[1]}-S{g[2]}"
        if f == "M":
            if not 1 <= int(g[1]) <= 12:
                continue
            return f, f"{g[0]}-{g[1]}"
        if f == "Q":
            return f, f"{g[0]}-Q{g[1]}"
        if f == "S":
            return f, f"{g[0]}-S{g[1]}"
        return f, g[0]
    raise ValueError(f"Cannot parse period '{value}'" + (f" (frequency {freq})" if freq else ""))


def canonical_period(value: str, freq: str) -> str:
    return parse_period(value, freq)[1]


def to_ecos_period(period: str, freq: str) -> str:
    """Canonical period → ECOS query format (2026-01 → 202601, 2026-Q1 → 2026Q1)."""
    _, canonical = parse_period(period, freq)
    return canonical.replace("-", "")


def ecos_to_canonical(value: str, freq: str) -> str:
    return canonical_period(value, freq)


def period_index(period: str, freq: str) -> int:
    """Sortable integer for a canonical period (reuses the ECOS period arithmetic)."""
    return period_to_index(freq, to_ecos_period(period, freq))


# ── Series ──────────────────────────────────────────────────────────


@dataclass
class Observation:
    period: str
    value: float | None
    status: str | None = None  # provider observation status (e.g. SDMX OBS_STATUS)
    source_value: float | None = None  # level before a server-side transform (yoy/pop)


@dataclass
class Provenance:
    """Where a series came from and what was done to it."""

    provider: str  # ECOS | OECD | IMF | BIS
    agency: str
    dataflow: str
    series_key: str
    retrieved_at: str
    query_url: str | None = None  # never contains credentials
    web_url: str | None = None
    dataflow_name: str | None = None
    attribution: str | None = None
    transformations: list[str] = field(default_factory=list)

    def citation(self, title: str | None = None) -> str:
        name = f"'{title}'" if title else self.dataflow_name or self.dataflow
        return f"Source: {self.agency}, {name}, dataset {self.dataflow}, series {self.series_key}, retrieved {self.retrieved_at}"


@dataclass
class CanonicalSeries:
    provider: str
    dataflow: str
    series_key: str
    freq: str
    title: str
    observations: list[Observation]
    provenance: Provenance
    ref_area: str | None = None
    unit: str | None = None
    unit_label: str | None = None
    unit_mult: int = 0
    base_period: str | None = None
    adjustment: str | None = None
    concept_id: str | None = None
    dimensions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    total_count: int | None = None

    @property
    def series_id(self) -> str:
        return f"{self.provider}:{self.dataflow}:{self.series_key}"

    def points(self) -> list[tuple[str, float]]:
        return [(o.period, o.value) for o in self.observations if isinstance(o.value, int | float)]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["series_id"] = self.series_id
        return out


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")
