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
        return Check("country", "pass", "국가 지정 없음", {"ref_area": series.ref_area})
    if series.ref_area is None:
        return Check("country", "warn", "공급자가 국가 차원을 제공하지 않아 확인할 수 없습니다", {"expected": exp.country})
    if series.ref_area != exp.country:
        return Check(
            "country",
            "fail",
            f"요청 국가 {exp.country}와 다른 국가 {series.ref_area}의 데이터입니다",
            {"expected": exp.country, "actual": series.ref_area},
        )
    return Check("country", "pass", f"국가 {series.ref_area}", {"ref_area": series.ref_area})


def _check_frequency(series: CanonicalSeries, exp: Expectation) -> Check:
    bad = []
    for obs in series.observations:
        try:
            parse_period(obs.period, series.freq)
        except ValueError:
            bad.append(obs.period)
    if bad:
        return Check("frequency", "fail", f"주기 {series.freq}와 맞지 않는 시점 {len(bad)}개", {"periods": bad[:5]})
    if exp.freq and series.freq != exp.freq:
        return Check(
            "frequency", "fail", f"요청 주기 {exp.freq}와 다른 주기 {series.freq}", {"expected": exp.freq, "actual": series.freq}
        )
    return Check("frequency", "pass", f"주기 {series.freq}", {"freq": series.freq})


def _check_unit(series: CanonicalSeries, exp: Expectation) -> Check:
    observed = canonical_unit(series.unit)
    expected = canonical_unit(exp.unit)
    details = {"expected": expected, "actual": observed, "label": series.unit_label}
    if not expected:
        return Check("unit", "info", f"기대 단위 없음(관측 단위 {observed})", details)
    if observed is None:
        return Check("unit", "info", f"공급자가 단위를 명시하지 않아 카탈로그 선언({expected})을 따릅니다", details)
    if observed != expected and expected not in _COMPATIBLE_UNITS.get(observed, set()):
        return Check("unit", "fail", f"단위 불일치: 기대 {expected}, 실제 {observed}", details)
    if exp.base_period and series.base_period and not series.base_period.startswith(exp.base_period):
        return Check(
            "unit",
            "warn",
            f"지수 기준시점이 다릅니다: 기대 {exp.base_period}, 실제 {series.base_period}",
            {**details, "expected_base": exp.base_period, "actual_base": series.base_period},
        )
    if observed != expected:
        return Check("unit", "pass", f"공급자 단위 {observed}는 {expected}와 호환됩니다", details)
    base = f", 기준 {series.base_period}" if series.base_period else ""
    return Check("unit", "pass", f"단위 {observed}{base}", details)


def _check_scale(series: CanonicalSeries, exp: Expectation) -> Check:
    details = {"expected": exp.unit_mult, "actual": series.unit_mult}
    if exp.unit_mult is None or exp.unit_mult == series.unit_mult:
        return Check("scale", "pass", f"배수 10^{series.unit_mult}", details)
    factor = 10 ** (series.unit_mult - exp.unit_mult)
    return Check(
        "scale", "warn", f"배수가 다릅니다(10^{series.unit_mult} vs 10^{exp.unit_mult}): 값에 {factor:g}를 곱해야 비교됩니다", details
    )


def _check_period(series: CanonicalSeries, exp: Expectation) -> Check:
    periods = [o.period for o in series.observations]
    if not periods:
        return Check("period", "warn", "관측치가 없습니다", {})
    try:
        idx = [period_index(p, series.freq) for p in periods]
    except ValueError:
        return Check("period", "fail", "시점을 해석할 수 없습니다", {})
    details: dict[str, Any] = {"first": periods[0], "last": periods[-1], "count": len(periods)}
    if idx != sorted(idx):
        return Check("period", "fail", "시점이 시간순으로 정렬되어 있지 않습니다", details)
    outside = []
    for p, i in zip(periods, idx):
        try:
            if exp.start and i < period_index(exp.start, series.freq) or exp.end and i > period_index(exp.end, series.freq):
                outside.append(p)
        except ValueError:
            break
    if outside:
        return Check("period", "fail", f"요청 기간 밖 시점 {len(outside)}개", {**details, "outside": outside[:5]})
    return Check("period", "pass", f"{periods[0]} ~ {periods[-1]} ({len(periods)}개)", details)


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
        return Check("missing", "warn", f"결측 {len(nulls)}개, 중간 누락 시점 {len(gaps)}개", details)
    if lagging:
        details["latest"] = lagging
        return Check("missing", "info", f"최신 관측치는 {lagging}입니다(이후 시점은 아직 미발표일 수 있음)", details)
    return Check("missing", "pass", "결측 없음", details)


def _check_duplicate(series: CanonicalSeries, exp: Expectation) -> Check:
    seen: set[str] = set()
    dup = sorted({o.period for o in series.observations if o.period in seen or seen.add(o.period)})
    if dup:
        return Check("duplicate", "fail", f"중복 시점 {len(dup)}개", {"periods": dup[:10]})
    return Check("duplicate", "pass", "중복 없음", {})


def _check_revision(series: CanonicalSeries, store: RevisionStore | None) -> Check:
    if store is None:
        return Check("revision", "pass", "개정 이력 비활성화", {})
    previous = store.load(series.series_id)
    # Revisions concern the provider's own numbers, so compare raw values (before yoy/pop).
    current = {o.period: (o.source_value if o.source_value is not None else o.value) for o in series.observations}
    store.save(series.series_id, current, series.provenance.retrieved_at)
    if not previous:
        return Check("revision", "pass", "이전 조회 기록 없음(이번 값을 기준으로 저장)", {})
    revised = [
        {"period": p, "previous": previous["values"][p], "current": v}
        for p, v in current.items()
        if p in previous.get("values", {}) and previous["values"][p] != v
    ]
    if revised:
        return Check(
            "revision",
            "info",
            f"이전 조회({previous.get('retrieved_at')}) 이후 {len(revised)}개 시점 값이 개정되었습니다",
            {"revised": revised[:20], "previous_retrieved_at": previous.get("retrieved_at")},
        )
    return Check("revision", "pass", f"이전 조회({previous.get('retrieved_at')}) 대비 개정 없음", {})


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
    ledger: ValidationLedger | None = None,
) -> dict[str, Any]:
    """Compare the same concept for one country across providers, period by period."""
    series_list = [s for s in series_list if s.points()]
    if len(series_list) < 2:
        return {"status": "not_enough_sources", "sources": [s.provider for s in series_list], "records": []}

    notes: list[str] = []
    units = {s.provider: canonical_unit(s.unit) for s in series_list}
    concept_unit = canonical_unit(unit) or next((u for u in units.values() if u), None)
    for provider, u in units.items():
        if u and concept_unit and u != concept_unit and concept_unit not in _COMPATIBLE_UNITS.get(u, set()):
            return {
                "status": "not_comparable",
                "reason": f"단위가 다릅니다: {units}",
                "records": [],
            }

    target = coarsest_cycle([s.freq for s in series_list])
    converted: dict[str, list[tuple[str, float]]] = {}
    for s in series_list:
        points = s.points()
        if s.freq != target:
            if not can_convert(s.freq, target):
                return {"status": "not_comparable", "reason": f"{s.provider} 주기 {s.freq}를 {target}로 맞출 수 없습니다", "records": []}
            ecos_points = [(to_ecos_period(p, s.freq), v) for p, v in points]
            points = [(ecos_to_canonical(p, target), v) for p, v in convert_frequency(ecos_points, s.freq, target, "mean")]
            notes.append(f"{s.provider}: {s.freq} → {target} 평균으로 변환")
        converted[s.provider] = _rescale(points, s.unit_mult, series_list[0].unit_mult)
        if s.unit_mult != series_list[0].unit_mult:
            notes.append(f"{s.provider}: 배수 10^{s.unit_mult} → 10^{series_list[0].unit_mult}로 환산")

    method = "direct"
    tolerance = _TOLERANCES.get(concept_unit or "", _DEFAULT_TOLERANCE)
    bases = {s.provider: s.base_period for s in series_list}
    if concept_unit == "IX" and len({b for b in bases.values()}) > 1:
        common = sorted(set.intersection(*(set(dict(p)) for p in converted.values())))
        if not common:
            return {"status": "not_comparable", "reason": "공통 시점이 없어 재기준화할 수 없습니다", "records": []}
        anchor = common[0]
        for provider, points in converted.items():
            base_value = dict(points)[anchor]
            converted[provider] = [(p, v / base_value * 100) for p, v in points] if base_value else points
        method = f"rebased:{anchor}=100"
        tolerance = _REBASED_INDEX_TOLERANCE
        notes.append(f"지수 기준시점이 달라({bases}) {anchor}=100으로 재기준화해 비교")

    adjustments = {s.provider: s.adjustment for s in series_list if s.adjustment}
    if len(set(adjustments.values())) > 1:
        notes.append(f"계절조정 여부가 다릅니다({adjustments}) — 차이가 날 수 있습니다")

    reference = series_list[0].provider
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
            by_provider[p] = {
                "difference": d,
                "rel_difference_pct": round(rel * 100, 4) if rel is not None else None,
                "status": status_p,
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
        agreement[provider] = {
            "vs": reference,
            **{k: sum(1 for x in stats if x["status"] == k) for k in ("match", "within_tolerance", "mismatch")},
            "max_abs_difference": max((abs(x["difference"]) for x in stats), default=None),
            "mean_abs_difference": round(sum(abs(x["difference"]) for x in stats) / len(stats), 6) if stats else None,
        }
    if not records:
        verdict = "no_common_periods"
    elif counts["mismatch"] == 0:
        verdict = "consistent" if counts["within_tolerance"] == 0 else "consistent_within_tolerance"
    elif counts["mismatch"] <= len(records) // 10:
        verdict = "minor_differences"
    else:
        verdict = "inconsistent"
    return {
        "status": verdict,
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
