import pytest

from global_economic_statistical_mcp.model import (
    canonical_unit,
    parse_period,
    period_index,
    to_ecos_period,
)


@pytest.mark.parametrize(
    ("raw", "freq", "expected"),
    [
        ("202601", "M", "2026-01"),  # ECOS
        ("2026-01", "M", "2026-01"),  # OECD / BIS
        ("2026-M01", "M", "2026-01"),  # IMF SDMX 3.0
        ("2026Q1", "Q", "2026-Q1"),
        ("2026-Q1", "Q", "2026-Q1"),
        ("2026S2", "S", "2026-S2"),
        ("2026", "A", "2026"),
        ("20260115", "D", "2026-01-15"),
        ("2026-01-15", "D", "2026-01-15"),
        ("202601S2", "SM", "2026-01-S2"),
    ],
)
def test_parse_period_accepts_every_dialect(raw, freq, expected):
    assert parse_period(raw, freq) == (freq, expected)
    assert parse_period(raw) == (freq, expected)


@pytest.mark.parametrize("raw", ["2026-13", "20260230", "abc", "2026Q5"])
def test_parse_period_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_period(raw)


def test_round_trip_to_ecos_and_ordering():
    assert to_ecos_period("2026-01", "M") == "202601"
    assert to_ecos_period("2026-Q3", "Q") == "2026Q3"
    assert to_ecos_period("2026-01-15", "D") == "20260115"
    assert period_index("2026-02", "M") == period_index("2026-01", "M") + 1
    assert period_index("2027-Q1", "Q") == period_index("2026-Q4", "Q") + 1


def test_canonical_unit_equivalents():
    assert canonical_unit("PA") == "PC_PA"
    assert canonical_unit("PT_LF_SUB") == "PC"
    assert canonical_unit("KRW") == "XDC"
    assert canonical_unit(None) is None
