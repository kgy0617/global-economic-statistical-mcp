"""Validation layer: single-series checks, cross-validation and the ledger."""

import pytest

from global_economic_statistical_mcp.model import (
    CanonicalSeries,
    Observation,
    Provenance,
)
from global_economic_statistical_mcp.storage import RevisionStore, ValidationLedger
from global_economic_statistical_mcp.validation import (
    Expectation,
    cross_validate,
    validate_series,
)


def make(provider="ECOS", points=None, freq="M", unit="IX", area="KR", base=None, mult=0, adjustment=None, key="K"):
    points = points if points is not None else {"2026-01": 100.0, "2026-02": 101.0, "2026-03": 102.0}
    return CanonicalSeries(
        provider=provider,
        dataflow="FLOW",
        series_key=key,
        freq=freq,
        title="t",
        observations=[Observation(p, v) for p, v in points.items()],
        provenance=Provenance(provider, "agency", "FLOW", key, "2026-09-26T00:00:00+09:00"),
        ref_area=area,
        unit=unit,
        unit_mult=mult,
        base_period=base,
        adjustment=adjustment,
    )


def checks(series, **exp):
    report = validate_series(series, Expectation(**exp))
    return report, {c.check: c.status for c in report.checks}


def test_clean_series_passes_every_check():
    report, status = checks(make(), country="KR", freq="M", unit="IX", unit_mult=0, start="2026-01", end="2026-03")
    assert report.status == "pass"
    assert set(status) == {"country", "frequency", "unit", "scale", "period", "missing", "duplicate", "revision"}


def test_country_mismatch_fails():
    report, status = checks(make(area="AO"), country="KR")
    assert status["country"] == "fail" and report.status == "fail"


def test_unit_checks():
    assert checks(make(unit="PC_PA"), unit="IX")[1]["unit"] == "fail"
    assert checks(make(unit="PC"), unit="PC_YOY")[1]["unit"] == "pass"  # less specific but compatible
    assert checks(make(unit=None), unit="PC_PA")[1]["unit"] == "info"  # provider does not state a unit
    assert checks(make(base="2010"), unit="IX", base_period="2020")[1]["unit"] == "warn"


def test_scale_and_frequency():
    assert checks(make(mult=6), unit_mult=9)[1]["scale"] == "warn"
    assert checks(make(freq="M"), freq="Q")[1]["frequency"] == "fail"


def test_period_outside_window_fails():
    assert checks(make(), start="2026-02", end="2026-03")[1]["period"] == "fail"


def test_missing_values_gaps_and_lag():
    assert checks(make(points={"2026-01": 1.0, "2026-03": 3.0}))[1]["missing"] == "warn"
    assert checks(make(points={"2026-01": 1.0, "2026-02": None}))[1]["missing"] == "warn"
    assert checks(make(), end="2026-06")[1]["missing"] == "info"


def test_duplicates_fail():
    series = make()
    series.observations.append(Observation("2026-02", 101.0))
    assert checks(series)[1]["duplicate"] == "fail"


def test_revisions_are_detected_against_the_last_snapshot(tmp_path):
    store = RevisionStore(root=tmp_path, persist=True)
    first = validate_series(make(), Expectation(), store)
    assert {c.check: c.status for c in first.checks}["revision"] == "pass"
    revised = make(points={"2026-01": 100.0, "2026-02": 101.5, "2026-03": 102.0})
    report = validate_series(revised, Expectation(), RevisionStore(root=tmp_path, persist=True))
    revision = next(c for c in report.checks if c.check == "revision")
    assert revision.status == "info"
    assert revision.details["revised"] == [{"period": "2026-02", "previous": 101.0, "current": 101.5}]


def test_revision_compares_raw_values_not_transformed_ones(tmp_path):
    store = RevisionStore(root=tmp_path, persist=False)
    transformed = make()
    for obs in transformed.observations:
        obs.source_value, obs.value = obs.value, 2.5  # e.g. yoy computed by the server
    validate_series(transformed, Expectation(), store)
    report = validate_series(make(), Expectation(), store)
    assert next(c for c in report.checks if c.check == "revision").status == "pass"


# ── Cross-validation ────────────────────────────────────────────────


def test_cross_validation_records_values_and_differences():
    ledger = ValidationLedger(persist=False)
    ecos = make("ECOS", {"2026-07": 119.77, "2026-08": 120.05})
    imf = make("IMF", {"2026-07": 119.77, "2026-08": 120.05})
    result = cross_validate([ecos, imf], concept_id="CPI", country="KR", unit="IX", ledger=ledger)
    assert result["status"] == "consistent"
    record = result["records"][-1]
    assert record == {
        **record,
        "concept_id": "CPI",
        "country": "KR",
        "period": "2026-08",
        "unit": "IX",
        "frequency": "M",
        "values": {"ECOS": 120.05, "IMF": 120.05},
        "difference": {"IMF": 0.0},
        "status": "match",
    }
    assert result["agreement"]["IMF"]["match"] == 2
    assert len(ledger.records()) == 2


def test_cross_validation_rebases_indices_with_different_bases():
    ecos = make("ECOS", {"2026-01": 110.0, "2026-02": 121.0}, base="2020")
    oecd = make("OECD", {"2026-01": 120.0, "2026-02": 132.0}, base="2015")
    result = cross_validate([ecos, oecd], concept_id="CPI", country="KR", unit="IX")
    assert result["method"] == "rebased:2026-01=100"
    assert result["status"] == "consistent"
    assert result["records"][1]["values"] == {"ECOS": 110.0, "OECD": 110.0}


def test_cross_validation_converts_frequency_and_flags_mismatch():
    daily = make("ECOS", {"2026-01-05": 2.5, "2026-01-20": 2.5, "2026-02-10": 2.75}, freq="D", unit="PC_PA")
    monthly = make("BIS", {"2026-01": 2.5, "2026-02": 3.0}, unit="PC_PA")
    result = cross_validate([daily, monthly], concept_id="POLICY_RATE", country="KR", unit="PC_PA")
    assert result["frequency"] == "M"
    statuses = {r["period"]: r["by_provider"]["BIS"]["status"] for r in result["records"]}
    assert statuses == {"2026-01": "match", "2026-02": "mismatch"}
    assert result["status"] == "inconsistent"


def test_cross_validation_refuses_incompatible_units():
    result = cross_validate([make("A", unit="IX"), make("B", unit="PC_PA")], concept_id="X", country="KR")
    assert result["status"] == "not_comparable"


def test_cross_validation_notes_seasonal_adjustment_differences():
    result = cross_validate(
        [make("ECOS", adjustment="NSA", unit="PC"), make("OECD", adjustment="SA", unit="PC")],
        concept_id="UNEMPLOYMENT_RATE", country="KR", unit="PC",
    )
    assert any("계절조정" in n for n in result["notes"])


def test_ledger_summary_per_provider_pair(tmp_path):
    ledger = ValidationLedger(root=tmp_path, persist=True)
    cross_validate([make("ECOS"), make("IMF"), make("OECD", {"2026-01": 100.0, "2026-02": 105.0, "2026-03": 102.0})],
                   concept_id="CPI", country="KR", unit="IX", ledger=ledger)
    pairs = {tuple(p["providers"]): p for p in ValidationLedger(root=tmp_path, persist=True).summary()["pairs"]}
    assert pairs[("ECOS", "IMF")]["agreement_rate"] == 1.0
    assert pairs[("ECOS", "OECD")]["agreement_rate"] == pytest.approx(2 / 3, abs=1e-4)
