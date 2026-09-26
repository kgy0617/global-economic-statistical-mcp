"""Configuration: ECOS settings, period arithmetic and shared constants."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

SAMPLE_API_KEY = "sample"


def load_api_key() -> str:
    """Read ECOS_API_KEY, falling back to the sample key when unset or blank."""
    return (os.getenv("ECOS_API_KEY") or "").strip() or SAMPLE_API_KEY


# ECOS API Configuration
ECOS_BASE_URL = "https://ecos.bok.or.kr/api"
ECOS_API_KEY = load_api_key()
ECOS_RESPONSE_TYPE = "json"

# Default pagination
DEFAULT_START_COUNT = 1
DEFAULT_END_COUNT = 100
DEFAULT_LANGUAGE = "kr"
SAMPLE_KEY_MAX_COUNT = 10

# HTTP behaviour
HTTP_TIMEOUT_SECONDS = 30.0
HTTP_MAX_RETRIES = 2  # retries after the first attempt (3 attempts total)
HTTP_RETRY_BACKOFF_SECONDS = 0.5
METADATA_CACHE_TTL_SECONDS = 6 * 60 * 60
METADATA_CACHE_MAX_ENTRIES = 512
KEY_STATISTICS_CACHE_TTL_SECONDS = 60 * 60

# Default look-back windows when no dates are given
DEFAULT_RECENT_YEARS = 2
DEFAULT_DAILY_RECENT_DAYS = 90

# ECOS publishes on Korean dates; "today" must not depend on the host timezone.
# Korea has no daylight saving time, so a fixed offset is exact (and needs no tzdata).
KST = timezone(timedelta(hours=9), "KST")


def today_kst() -> date:
    return datetime.now(KST).date()


# Valid cycle values for StatisticSearch
VALID_CYCLES = {"A", "S", "Q", "M", "SM", "D"}
CYCLE_DESCRIPTIONS = {
    "A": "연간 (Annual) — 포맷: YYYY (예: 2024)",
    "S": "반기 (Semi-annual) — 포맷: YYYYS1 / YYYYS2 (예: 2024S1)",
    "Q": "분기 (Quarterly) — 포맷: YYYYQ1 ~ YYYYQ4 (예: 2024Q1)",
    "M": "월간 (Monthly) — 포맷: YYYYMM (예: 202401)",
    "SM": "반월 (Semi-monthly) — 포맷: YYYYMMS1 / YYYYMMS2 (예: 202401S1)",
    "D": "일간 (Daily) — 포맷: YYYYMMDD (예: 20240101)",
}

# Number of periods per year, used for year-over-year transforms
PERIODS_PER_YEAR = {"A": 1, "S": 2, "Q": 4, "M": 12, "SM": 24}

# Date validation patterns per cycle
CYCLE_DATE_FORMATS: dict[str, tuple[str, str]] = {
    "A": (r"^\d{4}$", "YYYY (예: '2024')"),
    "S": (r"^\d{4}S[12]$", "YYYYS1 또는 YYYYS2 (예: '2024S1')"),
    "Q": (r"^\d{4}Q[1-4]$", "YYYYQ1 ~ YYYYQ4 (예: '2024Q1')"),
    "M": (r"^\d{4}(0[1-9]|1[0-2])$", "YYYYMM (예: '202401')"),
    "SM": (r"^\d{4}(0[1-9]|1[0-2])S[12]$", "YYYYMMS1 또는 YYYYMMS2 (예: '202401S1')"),
    "D": (r"^\d{4}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$", "YYYYMMDD (예: '20240101')"),
}


def validate_date_format(cycle: str, date_str: str) -> tuple[bool, str]:
    """Validate that date_str matches the required format for cycle.

    Returns:
        (is_valid, expected_format_description)
    """
    cycle = cycle.upper()
    rule = CYCLE_DATE_FORMATS.get(cycle)
    if not rule:
        return False, f"지원되지 않는 주기입니다: '{cycle}'"
    pattern, desc = rule
    value = str(date_str).strip()
    if not re.match(pattern, value):
        return False, desc
    if cycle == "D":
        try:
            _parse_day(value)
        except ValueError:
            return False, f"{desc} — 존재하지 않는 날짜입니다"
    return True, ""


# ── Period arithmetic ───────────────────────────────────────────────


def _parse_day(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def period_to_index(cycle: str, value: str) -> int:
    """Convert a period string (e.g. '2024Q3') to a sortable integer index."""
    cycle = cycle.upper()
    value = value.strip()
    year = int(value[:4])
    if cycle == "A":
        return year
    if cycle == "S":
        return year * 2 + int(value[5]) - 1
    if cycle == "Q":
        return year * 4 + int(value[5]) - 1
    if cycle == "M":
        return year * 12 + int(value[4:6]) - 1
    if cycle == "SM":
        return (year * 12 + int(value[4:6]) - 1) * 2 + int(value[7]) - 1
    if cycle == "D":
        return _parse_day(value).toordinal()
    raise ValueError(f"지원되지 않는 주기입니다: '{cycle}'")


def index_to_period(cycle: str, index: int) -> str:
    """Inverse of period_to_index."""
    cycle = cycle.upper()
    if cycle == "A":
        return f"{index:04d}"
    if cycle == "S":
        return f"{index // 2:04d}S{index % 2 + 1}"
    if cycle == "Q":
        return f"{index // 4:04d}Q{index % 4 + 1}"
    if cycle == "M":
        return f"{index // 12:04d}{index % 12 + 1:02d}"
    if cycle == "SM":
        month_index, half = divmod(index, 2)
        return f"{month_index // 12:04d}{month_index % 12 + 1:02d}S{half + 1}"
    if cycle == "D":
        return date.fromordinal(index).strftime("%Y%m%d")
    raise ValueError(f"지원되지 않는 주기입니다: '{cycle}'")


def shift_period(cycle: str, value: str, periods: int) -> str:
    """Shift a period string by a number of periods (days for cycle 'D')."""
    return index_to_period(cycle, period_to_index(cycle, value) + periods)


def current_period(cycle: str, today: date | None = None) -> str:
    """Return the period string containing today (Korean date by default)."""
    today = today or today_kst()
    cycle = cycle.upper()
    if cycle == "A":
        return f"{today.year}"
    if cycle == "S":
        return f"{today.year}S{1 if today.month <= 6 else 2}"
    if cycle == "Q":
        return f"{today.year}Q{(today.month - 1) // 3 + 1}"
    if cycle == "M":
        return f"{today.year}{today.month:02d}"
    if cycle == "SM":
        return f"{today.year}{today.month:02d}S{1 if today.day <= 15 else 2}"
    if cycle == "D":
        return today.strftime("%Y%m%d")
    raise ValueError(f"지원되지 않는 주기입니다: '{cycle}'")


def period_start_date(cycle: str, value: str) -> date:
    """First calendar day of an ECOS period."""
    cycle = cycle.upper()
    value = value.strip()
    year = int(value[:4])
    if cycle == "A":
        return date(year, 1, 1)
    if cycle == "S":
        return date(year, 1 if value[5] == "1" else 7, 1)
    if cycle == "Q":
        return date(year, (int(value[5]) - 1) * 3 + 1, 1)
    if cycle == "M":
        return date(year, int(value[4:6]), 1)
    if cycle == "SM":
        return date(year, int(value[4:6]), 1 if value[7] == "1" else 16)
    if cycle == "D":
        return _parse_day(value)
    raise ValueError(f"지원되지 않는 주기입니다: '{cycle}'")


def period_end_date(cycle: str, value: str) -> date:
    """Last calendar day of an ECOS period."""
    if cycle.upper() == "D":
        return _parse_day(value.strip())
    return period_start_date(cycle, shift_period(cycle, value, 1)) - timedelta(days=1)


def convert_period(value: str, from_cycle: str, to_cycle: str) -> str:
    """Map a period to the (coarser or finer) period of to_cycle containing its first day."""
    return current_period(to_cycle, period_start_date(from_cycle, value))


# Accepted spellings for dates given without a cycle (compare_series, flexible inputs)
_ANY_PERIOD_PATTERNS: list[tuple[str, str]] = [
    (r"^(\d{4})$", "A"),
    (r"^(\d{4})-?S([12])$", "S"),
    (r"^(\d{4})-?Q([1-4])$", "Q"),
    (r"^(\d{4})-?(0[1-9]|1[0-2])$", "M"),
    (r"^(\d{4})-?(0[1-9]|1[0-2])-?S([12])$", "SM"),
    (r"^(\d{4})-?(0[1-9]|1[0-2])-?(0[1-9]|[12]\d|3[01])$", "D"),
]


def parse_any_period(value: str) -> tuple[str, str]:
    """Detect the cycle of a date string and return (cycle, canonical ECOS value).

    Accepts ECOS forms (2024, 2024Q1, 202401, 20240115, ...) and ISO-like forms
    (2024-01, 2024-01-15, 2024-Q1).
    """
    text = str(value).strip().upper()
    for pattern, cycle in _ANY_PERIOD_PATTERNS:
        match = re.match(pattern, text)
        if match:
            canonical = "".join(match.groups())
            if cycle == "S":
                canonical = f"{match.group(1)}S{match.group(2)}"
            elif cycle == "Q":
                canonical = f"{match.group(1)}Q{match.group(2)}"
            elif cycle == "SM":
                canonical = f"{match.group(1)}{match.group(2)}S{match.group(3)}"
            is_valid, _ = validate_date_format(cycle, canonical)
            if is_valid:
                return cycle, canonical
    raise ValueError(f"날짜 형식을 해석할 수 없습니다: '{value}'")


def to_cycle(value: str, cycle: str, bound: str = "start") -> str:
    """Normalize a date string of any supported form to the given cycle.

    bound='start' maps to the period containing the first day of the given period,
    bound='end' to the period containing its last day (so '2024' → '202412' for M).
    """
    cycle = cycle.upper()
    text = str(value).strip()
    if validate_date_format(cycle, text)[0]:
        return text
    source_cycle, canonical = parse_any_period(text)
    day = (
        period_start_date(source_cycle, canonical)
        if bound == "start"
        else period_end_date(source_cycle, canonical)
    )
    return current_period(cycle, day)


def get_default_date_range(
    cycle: str,
    recent_years: int | None = None,
    today: date | None = None,
) -> tuple[str, str]:
    """Calculate default start and end dates relative to today.

    Args:
        cycle: Period cycle (A, S, Q, M, SM, D)
        recent_years: Number of years to look back. When None, daily series look back
            DEFAULT_DAILY_RECENT_DAYS days and all other cycles DEFAULT_RECENT_YEARS years.
        today: Reference date (defaults to today's Korean date).

    Returns:
        (start_date, end_date) in valid format for cycle
    """
    today = today or today_kst()
    cycle = cycle.upper()
    end = current_period(cycle, today)

    if cycle == "D":
        if recent_years is None:
            start_day = today - timedelta(days=DEFAULT_DAILY_RECENT_DAYS)
        else:
            years = max(1, recent_years)
            try:
                start_day = today.replace(year=today.year - years)
            except ValueError:  # Feb 29 in a non-leap target year
                start_day = today.replace(year=today.year - years, day=28)
        return start_day.strftime("%Y%m%d"), end

    years = max(1, recent_years if recent_years is not None else DEFAULT_RECENT_YEARS)
    return shift_period(cycle, end, -years * PERIODS_PER_YEAR[cycle]), end


# ECOS error code descriptions
ECOS_ERROR_MAP = {
    "INFO-100": "인증키가 유효하지 않습니다. ECOS_API_KEY 환경변수를 확인하세요.",
    "INFO-200": "해당 조건에 맞는 데이터가 없습니다.",
    "ERROR-100": "필수 입력값이 누락되었습니다.",
    "ERROR-101": "주기와 날짜 형식이 일치하지 않습니다. (예: 분기는 2024Q1, 월은 202401)",
    "ERROR-200": "파일 타입 오류입니다.",
    "ERROR-300": "조회건수 값이 누락되었습니다.",
    "ERROR-301": "조회건수 오류입니다. (참고: sample 키는 1회 최대 10건만 조회 가능합니다.)",
    "ERROR-400": "조회 범위가 너무 넓어 60초 타임아웃이 발생했습니다. 날짜 범위를 줄이거나 항목코드를 지정하세요.",
    "ERROR-500": "한국은행 ECOS 서버 내부 오류가 발생했습니다.",
    "ERROR-600": "한국은행 DB 연결 오류가 발생했습니다.",
    "ERROR-601": "한국은행 SQL 오류가 발생했습니다.",
    "ERROR-602": "API 일일 호출 한도를 초과했습니다. 잠시 후 다시 시도하세요.",
}
