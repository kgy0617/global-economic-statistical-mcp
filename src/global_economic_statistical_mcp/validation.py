"""Validation layer: checks every canonical series and cross-checks sources.

Single-series checks (each recorded as pass / info / warn / fail):

- ``country``    the series' reference area is the requested country
- ``frequency``  the series frequency is the requested one and every period parses for it
- ``unit``       the unit (and index base period) is compatible with the concept
- ``scale``      the unit multiplier (power of ten) is the expected one
- ``period``     periods are ordered and inside the requested window
- ``missing``    null values, gaps between periods, and not-yet-published periods
- ``duplicate``  the same period does not appear twice
- ``revision``   values that changed since the last retrieval of the same series

Cross-validation aligns several sources for one concept and country (frequency, scale,
index base) and records, per period, every provider's value and the differences — e.g.::

    concept CPI · country KR · period 2026-08 · unit IX · frequency M
    ECOS = 120.05, IMF = 120.05, difference = 0 → match

Each comparison is then classified:

- ``MATCH``       identical or within tolerance
- ``DIFFER``      a real difference with a verifiable cause (different seasonal adjustment,
                  the precision a provider publishes, or a documented known difference)
- ``UNRESOLVED``  a real difference nobody has explained yet; recorded, never hidden
- ``NOT_COMPARED`` fewer than two sources, or no common unit, frequency or period
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from global_economic_statistical_mcp.analytics import (
    can_convert,
    coarsest_cycle,
    convert_frequency,
)
from global_economic_statistical_mcp.catalog.concepts import known_difference
from global_economic_statistical_mcp.config import index_to_period
from global_economic_statistical_mcp.model import (
    CanonicalSeries,
    canonical_unit,
    ecos_to_canonical,
    now_kst_iso,
    parse_period,
    period_index,
    to_ecos_period,
)
from global_economic_statistical_mcp.storage import RevisionStore, ValidationLedger

STATUS_ORDER = {"pass": 0, "info": 1, "warn": 2, "fail": 3}

# Provider unit codes that are a less specific form of the expected unit.
_COMPATIBLE_UNITS: dict[str, set[str]] = {
    "PC": {"PC_YOY", "PC_POP", "PC"},
    "XDC": {"XDC_USD", "XDC"},
}

# Cross-validation tolerances: (absolute, relative) — a difference passes if within either.
_TOLERANCES: dict[str, tuple[float, float]] = {
    "PC_PA": (0.05, 0.0),
    "PC": (0.05, 0.0),
    "PC_YOY": (0.05, 0.0),
    "PC_POP": (0.05, 0.0),
    "IX": (0.0, 0.001),
    "XDC_USD": (0.0, 0.005),
}
_REBASED_INDEX_TOLERANCE = (0.0, 0.01)
AGGREGATION_LABELS = {"mean": "average", "sum": "sum", "last": "end-of-period value", "first": "start-of-period value"}
_DEFAULT_TOLERANCE = (0.0, 0.005)


@dataclass
class Check:
    check: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Expectation:
    country: str | None = None
    freq: str | None = None
    unit: str | None = None
    unit_mult: int | None = None
    base_period: str | None = None
    adjustment: str | None = None
    start: str | None = None
    end: str | None = None
    concept_id: str | None = None


@dataclass
class ValidationReport:
    series_id: str
    concept_id: str | None
    checks: list[Check]

    @property
    def status(self) -> str:
        return max((c.status for c in self.checks), key=STATUS_ORDER.__getitem__, default="pass")

    def compact(self) -> dict[str, Any]:
        """Status plus only the checks that need attention."""
        out: dict[str, Any] = {"status": self.status, "checks": {c.check: c.status for c in self.checks}}
        issues = [
            {"check": c.check, "status": c.status, "message": c.message}
            for c in self.checks
            if c.status in ("warn", "fail") or (c.status == "info" and c.check in ("revision", "unit", "missing"))
        ]
        if issues:
            out["issues"] = issues
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "series_id": self.series_id,
            "concept_id": self.concept_id,
            "status": self.status,
            "checks": [asdict(c) for c in self.checks],
        }


# ── Single-series checks ────────────────────────────────────────────


def _check_country(series: CanonicalSeries, exp: Expectation) -> Check:
    if not exp.country:
        return Check("country", "pass", "no country requested", {"ref_area": series.ref_area})
    if series.ref_area is None:
        return Check("country", "warn", "the provider publishes no country dimension, so it cannot be checked", {"expected": exp.country})
    if series.ref_area != exp.country:
        return Check(
            "country",
            "fail",
            f"data is for {series.ref_area}, not the requested {exp.country}",
            {"expected": exp.country, "actual": series.ref_area},
        )
    return Check("country", "pass", f"country {series.ref_area}", {"ref_area": series.ref_area})


def _check_frequency(series: CanonicalSeries, exp: Expectation) -> Check:
    bad = []
    for obs in series.observations:
        try:
            parse_period(obs.period, series.freq)
        except ValueError:
            bad.append(obs.period)
    if bad:
        return Check("frequency", "fail", f"{len(bad)} periods do not match frequency {series.freq}", {"periods": bad[:5]})
    if exp.freq and series.freq != exp.freq:
        return Check(
            "frequency", "fail", f"frequency {series.freq}, not the requested {exp.freq}", {"expected": exp.freq, "actual": series.freq}
        )
    return Check("frequency", "pass", f"frequency {series.freq}", {"freq": series.freq})


def _check_unit(series: CanonicalSeries, exp: Expectation) -> Check:
    observed = canonical_unit(series.unit)
    expected = canonical_unit(exp.unit)
    details = {"expected": expected, "actual": observed, "label": series.unit_label}
    if not expected:
        if observed is None:
            return Check("unit", "info", "the provider publishes no unit and none is expected", details)
        return Check("unit", "pass", f"unit {observed} (none expected)", details)
    if observed is None:
        return Check("unit", "info", f"the provider publishes no unit; using the catalog declaration ({expected})", details)
    if observed != expected and expected not in _COMPATIBLE_UNITS.get(observed, set()):
        return Check("unit", "fail", f"unit mismatch: expected {expected}, got {observed}", details)
    if exp.base_period and series.base_period and not series.base_period.startswith(exp.base_period):
        return Check(
            "unit",
            "warn",
            f"index base period differs: expected {exp.base_period}, got {series.base_period}",
            {**details, "expected_base": exp.base_period, "actual_base": series.base_period},
        )
    if observed != expected:
        return Check("unit", "pass", f"provider unit {observed} is compatible with {expected}", details)
    base = f", base {series.base_period}" if series.base_period else ""
    return Check("unit", "pass", f"unit {observed}{base}", details)


def _check_scale(series: CanonicalSeries, exp: Expectation) -> Check:
    details = {"expected": exp.unit_mult, "actual": series.unit_mult}
    if exp.unit_mult is None or exp.unit_mult == series.unit_mult:
        return Check("scale", "pass", f"multiplier 10^{series.unit_mult}", details)
    factor = 10 ** (series.unit_mult - exp.unit_mult)
    return Check(
        "scale", "warn", f"multiplier differs (10^{series.unit_mult} vs 10^{exp.unit_mult}): multiply values by {factor:g} to compare", details
    )


def _check_period(series: CanonicalSeries, exp: Expectation) -> Check:
    periods = [o.period for o in series.observations]
    if not periods:
        return Check("period", "warn", "no observations", {})
    try:
        idx = [period_index(p, series.freq) for p in periods]
    except ValueError:
        return Check("period", "fail", "periods cannot be parsed", {})
    details: dict[str, Any] = {"first": periods[0], "last": periods[-1], "count": len(periods)}
    if idx != sorted(idx):
        return Check("period", "fail", "periods are not in chronological order", details)
    outside = []
    for p, i in zip(periods, idx):
        try:
            if exp.start and i < period_index(exp.start, series.freq) or exp.end and i > period_index(exp.end, series.freq):
                outside.append(p)
        except ValueError:
            break
    if outside:
        return Check("period", "fail", f"{len(outside)} periods outside the requested window", {**details, "outside": outside[:5]})
    return Check("period", "pass", f"{periods[0]} to {periods[-1]} ({len(periods)} periods)", details)


def _check_missing(series: CanonicalSeries, exp: Expectation) -> Check:
    nulls = [o.period for o in series.observations if o.value is None]
    details: dict[str, Any] = {"null_values": nulls[:10], "null_count": len(nulls)}
    gaps: list[str] = []
    if series.freq != "D" and len(series.observations) > 1:
        try:
            idx = sorted({period_index(o.period, series.freq) for o in series.observations})
            present = set(idx)
            missing_idx = [i for i in range(idx[0], idx[-1] + 1) if i not in present]
            gaps = [ecos_to_canonical(index_to_period(series.freq, i), series.freq) for i in missing_idx]
        except ValueError:
            gaps = []
    details["gaps"] = gaps[:10]
    details["gap_count"] = len(gaps)
    lagging = None
    if exp.end and series.observations and not series.truncated:
        try:
            last = period_index(series.observations[-1].period, series.freq)
            if last < period_index(exp.end, series.freq):
                lagging = series.observations[-1].period
        except ValueError:
            pass
    if nulls or gaps:
        return Check("missing", "warn", f"{len(nulls)} missing values, {len(gaps)} gaps", details)
    if lagging:
        details["latest"] = lagging
        return Check("missing", "info", f"latest observation is {lagging} (later periods may not be published yet)", details)
    return Check("missing", "pass", "no missing values", details)


def _check_duplicate(series: CanonicalSeries, exp: Expectation) -> Check:
    seen: set[str] = set()
    dup = sorted({o.period for o in series.observations if o.period in seen or seen.add(o.period)})
    if dup:
        return Check("duplicate", "fail", f"{len(dup)} duplicate periods", {"periods": dup[:10]})
    return Check("duplicate", "pass", "no duplicates", {})


def _check_revision(series: CanonicalSeries, store: RevisionStore | None) -> Check:
    if store is None:
        return Check("revision", "pass", "revision tracking disabled", {})
    previous = store.load(series.series_id)
    # Revisions concern the provider's own numbers, so compare raw values (before yoy/pop).
    current = {o.period: (o.source_value if o.source_value is not None else o.value) for o in series.observations}
    store.save(series.series_id, current, series.provenance.retrieved_at)
    if not previous:
        return Check("revision", "pass", "no previous retrieval (these values are stored as the baseline)", {})
    revised = [
        {"period": p, "previous": previous["values"][p], "current": v}
        for p, v in current.items()
        if p in previous.get("values", {}) and previous["values"][p] != v
    ]
    if revised:
        return Check(
            "revision",
            "info",
            f"{len(revised)} values revised since the previous retrieval ({previous.get('retrieved_at')})",
            {"revised": revised[:20], "previous_retrieved_at": previous.get("retrieved_at")},
        )
    return Check("revision", "pass", f"no revisions since the previous retrieval ({previous.get('retrieved_at')})", {})


def validate_series(
    series: CanonicalSeries, expectation: Expectation, store: RevisionStore | None = None
) -> ValidationReport:
    checks = [
        _check_country(series, expectation),
        _check_frequency(series, expectation),
        _check_unit(series, expectation),
        _check_scale(series, expectation),
        _check_period(series, expectation),
        _check_missing(series, expectation),
        _check_duplicate(series, expectation),
        _check_revision(series, store),
    ]
    return ValidationReport(series.series_id, expectation.concept_id or series.concept_id, checks)


def report_records(report: ValidationReport, series: CanonicalSeries, run_id: str) -> list[dict[str, Any]]:
    return [
        {
            "kind": "series_validation",
            "run_id": run_id,
            "recorded_at": now_kst_iso(),
            "concept_id": report.concept_id,
            "country": series.ref_area,
            "provider": series.provider,
            "series_id": series.series_id,
            "status": report.status,
            "checks": {c.check: c.status for c in report.checks},
        }
    ]


# ── Cross-validation ────────────────────────────────────────────────


MATCH, DIFFER, UNRESOLVED, NOT_COMPARED = "MATCH", "DIFFER", "UNRESOLVED", "NOT_COMPARED"
_SEVERITY = {MATCH: 0, DIFFER: 1, UNRESOLVED: 2}


def _not_comparable(reason: str) -> dict[str, Any]:
    return {"status": "not_comparable", "validation_status": NOT_COMPARED, "reason": reason, "records": []}


def published_decimals(series: CanonicalSeries) -> int | None:
    """Most decimals any published value carries (None if values look computed, > 6)."""
    most = 0
    for _, value in series.points():
        text = repr(float(value))
        if "." not in text or "e" in text:  # nan, inf or exponent notation
            return None
        most = max(most, len(text.split(".")[1].rstrip("0")))
    return most if most <= 6 else None


def _rescale(points: list[tuple[str, float]], mult: int, target: int) -> list[tuple[str, float]]:
    if mult == target:
        return points
    factor = 10 ** (mult - target)
    return [(p, v * factor) for p, v in points]


def cross_validate(
    series_list: list[CanonicalSeries],
    *,
    concept_id: str | None,
    country: str | None,
    unit: str | None = None,
    aggregation: str = "mean",
    ledger: ValidationLedger | None = None,
) -> dict[str, Any]:
    """Compare the same concept for one country across providers, period by period.

    ``aggregation`` is how a higher-frequency source is brought to the common frequency:
    ``sum`` for flows (a monthly current account against a quarterly one), ``last`` for stocks.
    """
    series_list = [s for s in series_list if s.points()]
    if len(series_list) < 2:
        return {
            "status": "not_enough_sources",
            "validation_status": NOT_COMPARED,
            "sources": [s.provider for s in series_list],
            "records": [],
        }

    notes: list[str] = []
    units = {s.provider: canonical_unit(s.unit) for s in series_list}
    concept_unit = canonical_unit(unit) or next((u for u in units.values() if u), None)
    for provider, u in units.items():
        if u and concept_unit and u != concept_unit and concept_unit not in _COMPATIBLE_UNITS.get(u, set()):
            return _not_comparable(f"units differ: {units}")

    target = coarsest_cycle([s.freq for s in series_list])
    converted: dict[str, list[tuple[str, float]]] = {}
    for s in series_list:
        points = s.points()
        if s.freq != target:
            if not can_convert(s.freq, target):
                return _not_comparable(f"cannot convert {s.provider} frequency {s.freq} to {target}")
            ecos_points = [(to_ecos_period(p, s.freq), v) for p, v in points]
            points = [(ecos_to_canonical(p, target), v) for p, v in convert_frequency(ecos_points, s.freq, target, aggregation)]
            notes.append(f"{s.provider}: {s.freq} → {target} by {AGGREGATION_LABELS.get(aggregation, aggregation)}")
        converted[s.provider] = _rescale(points, s.unit_mult, series_list[0].unit_mult)
        if s.unit_mult != series_list[0].unit_mult:
            notes.append(f"{s.provider}: rescaled from 10^{s.unit_mult} to 10^{series_list[0].unit_mult}")

    method = "direct"
    tolerance = _TOLERANCES.get(concept_unit or "", _DEFAULT_TOLERANCE)
    bases = {s.provider: s.base_period for s in series_list}
    if concept_unit == "IX" and len({b for b in bases.values()}) > 1:
        common = sorted(set.intersection(*(set(dict(p)) for p in converted.values())))
        if not common:
            return _not_comparable("no common period to rebase on")
        anchor = common[0]
        for provider, points in converted.items():
            base_value = dict(points)[anchor]
            converted[provider] = [(p, v / base_value * 100) for p, v in points] if base_value else points
        method = f"rebased:{anchor}=100"
        tolerance = _REBASED_INDEX_TOLERANCE
        notes.append(f"index base periods differ ({bases}); rebased to {anchor}=100 for comparison")

    adjustments = {s.provider: s.adjustment for s in series_list if s.adjustment}
    if len(set(adjustments.values())) > 1:
        notes.append(f"seasonal adjustment differs ({adjustments}); values may differ for that reason")

    reference = series_list[0].provider
    by_name = {s.provider: s for s in series_list}
    # Half a unit of the last published digit, when values are compared as published.
    same_scale = method == "direct" and len({s.unit_mult for s in series_list}) == 1
    rounding = {
        s.provider: 0.5 * 10 ** -d for s in series_list if same_scale and (d := published_decimals(s)) is not None and d <= 2
    }

    def explain(provider: str, diff: float) -> str | None:
        ref, other = by_name[reference], by_name[provider]
        if ref.adjustment and other.adjustment and ref.adjustment != other.adjustment:
            return f"seasonal adjustment differs ({reference} {ref.adjustment}, {provider} {other.adjustment})"
        bound = rounding.get(reference, 0.0) + rounding.get(provider, 0.0)
        if bound and abs(diff) <= bound + 1e-9:
            coarse = [p for p in (reference, provider) if p in rounding]
            return "publication precision: " + ", ".join(f"{p} publishes {published_decimals(by_name[p])} decimals" for p in coarse)
        known = known_difference(concept_id, country, provider)
        return f"{known.explanation} ({known.evidence})" if known else None

    common_periods = sorted(set.intersection(*(set(dict(p)) for p in converted.values())))
    run_id = uuid.uuid4().hex[:12]
    recorded_at = now_kst_iso()
    abs_tol, rel_tol = tolerance
    records = []
    def classify(diff: float, ref_value: float) -> tuple[str, float | None]:
        rel = abs(diff) / abs(ref_value) if ref_value else (0.0 if diff == 0 else None)
        if abs(diff) <= 1e-9:
            return "match", rel
        if abs(diff) <= abs_tol or (rel is not None and rel <= rel_tol):
            return "within_tolerance", rel
        return "mismatch", rel

    for period in common_periods:
        values = {provider: dict(points)[period] for provider, points in converted.items()}
        ref_value = values[reference]
        diffs = {p: round(v - ref_value, 6) for p, v in values.items() if p != reference}
        by_provider = {}
        for p, d in diffs.items():
            status_p, rel = classify(d, ref_value)
            explanation = explain(p, d) if status_p == "mismatch" else None
            by_provider[p] = {
                "difference": d,
                "rel_difference_pct": round(rel * 100, 4) if rel is not None else None,
                "status": status_p,
                "validation_status": MATCH if status_p != "mismatch" else DIFFER if explanation else UNRESOLVED,
                **({"explanation": explanation} if explanation else {}),
            }
        statuses = [v["status"] for v in by_provider.values()]
        status = "mismatch" if "mismatch" in statuses else "within_tolerance" if "within_tolerance" in statuses else "match"
        max_abs = max((abs(d) for d in diffs.values()), default=0.0)
        max_rel = max_abs / abs(ref_value) if ref_value else (0.0 if max_abs == 0 else float("inf"))
        records.append(
            {
                "kind": "cross_validation",
                "run_id": run_id,
                "recorded_at": recorded_at,
                "concept_id": concept_id,
                "country": country,
                "period": period,
                "frequency": target,
                "unit": concept_unit,
                "method": method,
                "reference": reference,
                "values": {p: round(v, 6) for p, v in values.items()},
                "difference": diffs,
                "by_provider": by_provider,
                "max_abs_difference": round(max_abs, 6),
                "max_rel_difference_pct": round(max_rel * 100, 4) if max_rel != float("inf") else None,
                "status": status,
            }
        )
    if ledger is not None:
        ledger.append(records)

    counts = {k: sum(1 for r in records if r["status"] == k) for k in ("match", "within_tolerance", "mismatch")}
    agreement = {}
    for provider in converted:
        if provider == reference:
            continue
        stats = [r["by_provider"][provider] for r in records]
        classes = [x["validation_status"] for x in stats]
        worst = max(classes, key=_SEVERITY.__getitem__, default=MATCH)
        explanations = sorted({x["explanation"] for x in stats if "explanation" in x})
        rels = [x["rel_difference_pct"] for x in stats if x["rel_difference_pct"] is not None]
        agreement[provider] = {
            "vs": reference,
            "validation_status": worst,
            **{k: sum(1 for x in stats if x["status"] == k) for k in ("match", "within_tolerance", "mismatch")},
            "difference": {
                "absolute_max": max((abs(x["difference"]) for x in stats), default=None),
                "absolute_mean": round(sum(abs(x["difference"]) for x in stats) / len(stats), 6) if stats else None,
                "relative_max_pct": max(rels, default=None),
            },
            "investigation": {
                "status": {MATCH: "not_needed", DIFFER: "explained", UNRESOLVED: "unresolved"}[worst],
                **({"explanations": explanations} if explanations else {}),
                **(
                    {"unresolved_periods": [r["period"] for r, x in zip(records, stats) if x["validation_status"] == UNRESOLVED]}
                    if worst == UNRESOLVED
                    else {}
                ),
            },
        }
    if not records:
        verdict = "no_common_periods"
    elif counts["mismatch"] == 0:
        verdict = "consistent" if counts["within_tolerance"] == 0 else "consistent_within_tolerance"
    elif counts["mismatch"] <= len(records) // 10:
        verdict = "minor_differences"
    else:
        verdict = "inconsistent"
    overall = max((a["validation_status"] for a in agreement.values()), key=_SEVERITY.__getitem__, default=MATCH)
    return {
        "status": verdict,
        "validation_status": overall if records else NOT_COMPARED,
        "concept_id": concept_id,
        "country": country,
        "frequency": target,
        "unit": concept_unit,
        "method": method,
        "reference": reference,
        "sources": [
            {"provider": s.provider, "series_id": s.series_id, "freq": s.freq, "base_period": s.base_period, "adjustment": s.adjustment}
            for s in series_list
        ],
        "tolerance": {"absolute": abs_tol, "relative": rel_tol},
        "counts": {**counts, "periods": len(records)},
        "agreement": agreement,
        "notes": notes,
        "records": records,
        "run_id": run_id,
    }
