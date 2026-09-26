"""MCP Server for the Bank of Korea ECOS Open API, with an SDMX view of its metadata.

Workflow the tools are designed around (metadata first, then data):

    search_statistics  →  get_metadata  →  get_data
                     explain_indicator  ·  calculate_statistics  ·  compare_series

Tools (6개):
- search_statistics: 통계표·인기지표·100대 지표 검색 및 통계표 분류 탐색
- get_metadata: 통계표 구조(SDMX Dataflow/DSD/Codelist) 조회 — 항목코드·주기·수록기간·단위
- get_data: 시계열 조회 (인기지표 1-shot, 최신 우선, 증감률, compact/csv/json/sdmx)
- compare_series: 여러 시계열을 같은 주기로 정렬·비교하고 상관계수 계산
- calculate_statistics: 기술통계·증감률·CAGR·추세·변동성
- explain_indicator: 용어 정의 + 통계 설명자료 + 관련 통계표·SDMX 개념을 한 번에

Resources: ecos://popular-indicators, ecos://date-format-guide, ecos://sdmx/conventions
Prompts: macro-economic-briefing, analyze-economic-trend
CLI: ecos-mcp --check | --version
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ecos_mcp import sdmx
from ecos_mcp.analytics import (
    AGGREGATIONS,
    NORMALIZATIONS,
    align,
    can_convert,
    coarsest_cycle,
    convert_frequency,
    correlation_matrix,
    describe,
    normalize,
    rebase_index,
    scale_multiplier,
    unit_multiplier_from_name,
)
from ecos_mcp.client import EcosApiError, EcosClient
from ecos_mcp.concepts import CONCEPTS, all_concepts, get_concept, search_concepts
from ecos_mcp.config import (
    CYCLE_DATE_FORMATS,
    CYCLE_DESCRIPTIONS,
    DEFAULT_TIMESERIES_ROWS,
    ECOS_API_KEY,
    KST,
    POPULAR_INDICATORS,
    SAMPLE_API_KEY,
    VALID_CYCLES,
    _rank_popular_indicators,
    find_popular_indicator,
    get_default_date_range,
    match_popular_indicators,
    period_to_index,
    shift_period,
    to_cycle,
)
from ecos_mcp.items import search_items
from ecos_mcp.timeseries import (
    OUTPUT_FORMATS,
    TRANSFORMS,
    apply_transform,
    drop_unchanged,
    dumps,
    format_timeseries,
    group_series,
    series_key,
    series_label,
    to_number,
    transform_lookback,
)

try:
    __version__ = version("ecos-mcp")
except PackageNotFoundError:  # imported from a source tree without installation
    __version__ = "0.0.0+unknown"

DATA_FORMATS = OUTPUT_FORMATS | {"sdmx"}


# ── Lifespan: manage the shared EcosClient ─────────────────────────

@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    """Create and tear down the shared ECOS API client."""
    client = EcosClient()
    try:
        yield {"ecos_client": client}
    finally:
        await client.close()


# ── MCP Server ──────────────────────────────────────────────────────

mcp = MCPServer(
    name="ECOS MCP Server",
    version=__version__,
    description=(
        "한국은행 경제통계시스템(ECOS) Open API MCP 서버. 통계 메타데이터를 SDMX 구조로 제공하고, "
        "GDP·기준금리·물가·환율·통화량 등 한국 경제 통계를 조회·비교·분석합니다."
    ),
    instructions=(
        "메타데이터를 먼저 확인하고 알맞은 데이터를 조회하세요. "
        "1) 기준금리·성장률·물가(상승률)·환율·통화량·국고채·PPI 등 주요 거시지표는 get_data(indicator=...) 한 번으로 바로 조회할 수 있습니다 (SDMX 표준 개념 지원). "
        "2) 세부 품목(예: '쌀', '휘발유', '반도체', '전기료')이나 통계표는 search_statistics로 검색합니다. 항목코드가 나오면 get_data로 즉시 조회 가능합니다. "
        "3) 지표의 의미·작성방법은 explain_indicator, 요약통계·추세는 calculate_statistics, "
        "여러 지표의 관계는 compare_series를 사용합니다. "
        "4) 모든 통계 수치를 인용하거나 보고서를 작성할 때는 데이터 응답에 포함된 'evidence'(출처: 한국은행 ECOS, 통계표, 시계열 키, 조회시각, 링크)를 반드시 인용하세요."
    ),
    lifespan=lifespan,
)

REMOTE_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def _get_client(ctx: Context) -> EcosClient:
    """Extract the EcosClient from the lifespan context."""
    return ctx.request_context.lifespan_context["ecos_client"]


def _compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop null/empty fields, which make up much of ECOS metadata rows."""
    return [{k: v for k, v in r.items() if v not in (None, "")} for r in rows]


async def _call_ecos(coro: Any) -> Any:
    """Await an ECOS client call, surfacing API failures as MCP tool errors."""
    try:
        return await coro
    except EcosApiError as e:
        raise ToolError(str(e)) from e


def _normalize_transform(transform: str | None) -> str | None:
    value = (transform or "").strip().lower()
    if value in ("", "none", "raw"):
        return None
    if value not in TRANSFORMS:
        raise ToolError(
            f"지원되지 않는 transform입니다: '{transform}'. "
            "'yoy'(전년동기대비 %), 'pop'(직전 관측치 대비 %), 'none' 중 하나를 사용하세요."
        )
    return value


def _normalize_cycle(cycle: str | None) -> str:
    value = (cycle or "").strip().upper()
    if value not in VALID_CYCLES:
        valid_str = ", ".join(f"{k}: {v}" for k, v in CYCLE_DESCRIPTIONS.items())
        raise ToolError(f"유효하지 않은 주기(cycle)입니다: '{cycle}'. 허용 주기 목록:\n{valid_str}")
    return value


def _choice(value: str | None, allowed: set[str], name: str, default: str) -> str:
    choice = (value or default).strip().lower()
    if choice not in allowed:
        raise ToolError(f"지원되지 않는 {name}입니다: '{value}'. {', '.join(sorted(allowed))} 중 하나를 사용하세요.")
    return choice


def _unknown_indicator_error(indicator: str) -> ToolError:
    candidates = match_popular_indicators(indicator)
    if candidates:
        listed = ", ".join(f"'{p['id']}'({p['name']})" for p in candidates)
        return ToolError(
            f"'{indicator}'에 해당하는 지표가 여러 개입니다: {listed}. 더 구체적인 키워드나 id를 사용하세요."
        )
    valid_names = ", ".join(f"'{p['id']}'({p['name']})" for p in POPULAR_INDICATORS)
    return ToolError(
        f"'{indicator}'에 일치하는 인기 지표를 찾을 수 없습니다.\n"
        f"지원 지표 목록: {valid_names}\n"
        "다른 지표는 search_statistics로 통계표를 찾은 뒤 stat_code로 조회하세요."
    )


# ── Series resolution & loading (shared by get_data / calculate / compare) ──


class SeriesSpec(BaseModel):
    """One time series: a popular indicator, or a table code plus item codes."""

    indicator: str | None = Field(None, description="인기 지표 키워드 또는 id (예: '기준금리', 'inflation')")
    stat_code: str | None = Field(None, description="통계표코드 (indicator 대신 사용)")
    cycle: str | None = Field(None, description="주기 A/S/Q/M/SM/D (생략 시 통계표 기본 주기)")
    item_code1: str | None = None
    item_code2: str | None = None
    item_code3: str | None = None
    item_code4: str | None = None
    transform: str | None = Field(None, description="'yoy' | 'pop' | 'none' (생략 시 지표 기본값)")
    label: str | None = Field(None, description="결과에 표시할 이름")


@dataclass
class ResolvedSeries:
    stat_code: str
    cycle: str
    item_codes: list[str | None]
    transform: str | None = None
    changes_only: bool = False
    label: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


async def _default_cycle(client: EcosClient, stat_code: str) -> str:
    info = client.table_info(stat_code)
    if info and info.get("CYCLE") in VALID_CYCLES:
        return info["CYCLE"]
    items = await _call_ecos(client.list_statistic_items(stat_code, end_count=10))
    cycles = {r.get("CYCLE") for r in items["rows"]}
    for cycle in ("M", "Q", "A", "D", "S", "SM"):
        if cycle in cycles:
            return cycle
    raise ToolError(
        f"통계표 '{stat_code}'의 주기를 알 수 없습니다. cycle을 지정하거나 get_metadata로 확인하세요."
    )


async def _resolve_series(
    client: EcosClient,
    *,
    indicator: str | None,
    stat_code: str | None,
    cycle: str | None,
    item_codes: list[str | None],
    transform: str | None,
    changes_only: bool | None,
    label: str | None = None,
) -> ResolvedSeries:
    if indicator and indicator.strip():
        if stat_code:
            raise ToolError("indicator와 stat_code는 함께 쓸 수 없습니다. 둘 중 하나만 지정하세요.")
        preset = find_popular_indicator(indicator)
        concept = None
        item_match = None
        if not preset:
            ranked = _rank_popular_indicators(indicator)
            if ranked:
                # Ambiguous tie among popular indicators
                raise _unknown_indicator_error(indicator)

            concept = get_concept(indicator)
            if not concept:
                c_hits = search_concepts(indicator, limit=5)
                if len(c_hits) == 1:
                    concept = get_concept(c_hits[0]["concept_id"])
            if not concept:
                i_hits = search_items(indicator, limit=5)
                exact_items = [
                    it for it in i_hits
                    if it["item_name"] == indicator.strip()
                    or any(s == indicator.strip().lower() for s in it.get("synonyms", []))
                ]
                if len(exact_items) == 1:
                    item_match = exact_items[0]

        if not preset and not concept and not item_match:
            raise _unknown_indicator_error(indicator)

        if cycle or any(item_codes):
            target_name = (
                preset["id"]
                if preset
                else (concept.concept_id if concept else (item_match["item_name"] if item_match else indicator))
            )
            raise ToolError(
                f"indicator '{target_name}'는 고정 지표라 cycle·item_code를 바꿀 수 없습니다. "
                "다른 주기가 필요하면 search_statistics에서 해당 주기 지표를 찾거나 stat_code로 직접 조회하세요."
            )

        if preset:
            return ResolvedSeries(
                stat_code=preset["stat_code"],
                cycle=preset["cycle"],
                item_codes=[preset.get("item_code1"), preset.get("item_code2")],
                transform=_normalize_transform(preset.get("transform") if transform is None else transform),
                changes_only=preset.get("changes_only", False) if changes_only is None else changes_only,
                label=label or preset["name"],
                meta={
                    "indicator": preset["id"],
                    "indicator_name": preset["name"],
                    "stat_name": preset.get("stat_name"),
                },
            )
        elif concept:
            return ResolvedSeries(
                stat_code=concept.ecos.stat_code,
                cycle=concept.ecos.cycle,
                item_codes=[
                    concept.ecos.item_code1,
                    concept.ecos.item_code2,
                    concept.ecos.item_code3,
                    concept.ecos.item_code4,
                ],
                transform=_normalize_transform(
                    concept.ecos.default_transform if transform is None else transform
                ),
                changes_only=concept.ecos.changes_only if changes_only is None else changes_only,
                label=label or concept.name_ko,
                meta={
                    "concept_id": concept.concept_id,
                    "concept_name": concept.name_ko,
                    "sdmx_concept": concept.concept_id,
                    "sdmx_attributes": asdict(concept.sdmx),
                    "indicator": concept.concept_id.lower(),
                    "indicator_name": concept.name_ko,
                    "stat_name": concept.ecos.stat_name,
                },
            )
        else:
            assert item_match is not None
            return ResolvedSeries(
                stat_code=item_match["stat_code"],
                cycle=item_match["cycle"],
                item_codes=[item_match["item_code"]],
                transform=_normalize_transform(transform),
                changes_only=bool(changes_only),
                label=label or item_match["item_name"],
                meta={
                    "item_name": item_match["item_name"],
                    "stat_name": item_match.get("stat_name"),
                    "indicator": item_match["item_name"],
                    "indicator_name": item_match["item_name"],
                },
            )
    if not stat_code or not stat_code.strip():
        raise ToolError(
            "indicator 또는 stat_code가 필요합니다. 통계표코드는 search_statistics로 찾을 수 있습니다."
        )
    code = stat_code.strip()
    return ResolvedSeries(
        stat_code=code,
        cycle=_normalize_cycle(cycle) if cycle else await _default_cycle(client, code),
        item_codes=[c.strip() if c and c.strip() else None for c in item_codes],
        transform=_normalize_transform(transform),
        changes_only=bool(changes_only),
        label=label,
    )


def _date_range(
    cycle: str, start_date: str | None, end_date: str | None, recent_years: int | None
) -> tuple[str, str]:
    """Fill defaults and normalize dates of any supported form to the series cycle."""
    def_start, def_end = get_default_date_range(cycle, recent_years=recent_years)
    try:
        start = to_cycle(start_date, cycle, "start") if start_date else def_start
        end = to_cycle(end_date, cycle, "end") if end_date else def_end
    except ValueError as e:
        raise ToolError(
            f"{e}. 주기 '{cycle}'의 포맷은 {CYCLE_DATE_FORMATS[cycle][1]}이며, "
            "'2024', '2024-03', '2024Q1', '2024-03-15' 같은 형식도 자동 변환됩니다."
        ) from e
    if period_to_index(cycle, start) > period_to_index(cycle, end):
        raise ToolError(f"start_date('{start}')가 end_date('{end}')보다 늦습니다.")
    return start, end


async def _load_series(
    client: EcosClient,
    series: ResolvedSeries,
    start_date: str,
    end_date: str,
    *,
    language: str = "kr",
    start_count: int = 1,
    end_count: int = DEFAULT_TIMESERIES_ROWS,
    prefer_latest: bool = True,
    apply_changes_only: bool = True,
) -> dict[str, Any]:
    """Fetch StatisticSearch rows for a resolved series and post-process them."""
    if series.transform == "yoy" and series.cycle == "D":
        raise ToolError("일간(D) 데이터에는 transform='yoy'를 쓸 수 없습니다. 'pop'을 쓰거나 월간 통계표를 사용하세요.")
    codes = (list(series.item_codes) + [None] * 4)[:4]

    async def fetch(first: str, last: str, first_row: int, last_row: int, latest: bool) -> dict[str, Any]:
        return await _call_ecos(
            client.search_statistics(
                stat_code=series.stat_code,
                cycle=series.cycle,
                start_date=first,
                end_date=last,
                item_code1=codes[0],
                item_code2=codes[1],
                item_code3=codes[2],
                item_code4=codes[3],
                language=language,
                start_count=first_row,
                end_count=last_row,
                prefer_latest=latest,
            )
        )

    res = await fetch(start_date, end_date, start_count, end_count, prefer_latest)

    rows = res["rows"]
    if series.transform and rows:
        # The first periods of the page need earlier observations as their base.
        # Fetch only those periods, separately, so the page's counts stay accurate.
        lookback = transform_lookback(series.cycle, series.transform)
        first = min(str(r["TIME"]) for r in rows)
        last = max(str(r["TIME"]) for r in rows)
        base_last = shift_period(series.cycle, first, -1)
        if series.transform == "yoy":
            base_last = min(base_last, shift_period(series.cycle, last, -lookback))
        base = await fetch(
            shift_period(series.cycle, first, -lookback), base_last, 1, DEFAULT_TIMESERIES_ROWS, True
        )
        apply_transform(base["rows"] + rows, series.cycle, series.transform)
    if apply_changes_only and series.changes_only:
        res["rows"] = drop_unchanged(res["rows"])
    return res


def _series_summary(
    series: ResolvedSeries, start_date: str, end_date: str, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        **series.meta,
        "cycle": series.cycle,
        "start_date": start_date,
        "end_date": end_date,
    }
    if series.changes_only:
        summary["changes_only"] = True
    summary.update(extra or {})
    return summary


# ── Tool 1: 검색 ───────────────────────────────────────────────────

@mcp.tool(title="통계 검색·탐색", annotations=REMOTE_READ_ONLY, structured_output=False)
async def search_statistics(
    ctx: Context,
    query: str | None = None,
    scope: str = "all",
    parent_code: str | None = None,
    searchable_only: bool = True,
    limit: int = 20,
    language: str = "kr",
) -> str:
    """통계를 검색하거나 통계표 분류 트리를 탐색합니다. 데이터를 조회하기 전 첫 단계입니다.

    의미 검색과 세부 품목 검색을 완벽하게 지원합니다:
    - 영문/국문 동의어 검색 (예: "inflation" → CPI_INFLATION_RATE 개념 및 901Y009 매핑)
    - 세부 품목 검색 (예: "쌀", "휘발유", "반도체", "전기료", "사과" → 정확한 통계표코드 및 세부 항목코드 즉시 반환)
    - SDMX 표준 개념 체계 (REF_AREA=KR, FREQ, UNIT_MEASURE 및 IMF/OECD/BIS 국제기구 연동 슬롯)

    scope:
    - "all"(기본값): 표준 개념(concepts) + 세부 품목(items) + 인기 지표(indicators) + 통계표(tables) 통합 검색
    - "concepts": SDMX 표준 개념 체계(BOK_BASE_RATE, CPI_HEADLINE, GDP_REAL 등) 및 국제기구 매핑
    - "items": 세부 품목·상품·원자재(쌀, 쇠고기, 휘발유, 반도체, 만기별 국고채, 주요 통화 등)
    - "indicators": get_data(indicator=...)로 바로 조회되는 인기 지표 프리셋
    - "tables": 통계표만. query가 없으면 parent_code 분류의 하위 항목, 둘 다 없으면 최상위 분류
    - "key_statistics": 한국은행 100대 주요 경제지표의 최신값 (API 호출, query로 필터)

    Args:
        query: 검색어 (예: "쌀", "휘발유", "반도체", "inflation", "기준금리", "물가")
        scope: "all" | "concepts" | "items" | "indicators" | "tables" | "key_statistics"
        parent_code: 통계표 분류 코드. 지정하면 그 분류 아래에서만 검색하거나 하위 항목을 나열
        searchable_only: 통계표 검색 시 실제 조회 가능한 표(SRCH_YN='Y')만 (기본값 True)
        limit: 최대 반환 개수 (기본값 20)
        language: 응답 언어 — "kr" 또는 "en"

    Returns:
        concepts, items, indicators, tables, key_statistics 중 scope에 해당하는 검색 결과
    """
    scope = _choice(
        scope, {"all", "concepts", "items", "indicators", "tables", "key_statistics"}, "scope", "all"
    )
    client = _get_client(ctx)
    text = (query or "").strip()
    out: dict[str, Any] = {}

    if scope in ("all", "concepts"):
        matched_concepts = (
            search_concepts(text, limit=min(limit, 10)) if text else all_concepts()[: min(limit, 10)]
        )
        if matched_concepts:
            out["concepts"] = matched_concepts

    if scope in ("all", "items"):
        matched_items = search_items(text, limit=min(limit, 15)) if text else (search_items("", limit=min(limit, 20)) if scope == "items" else [])
        if matched_items:
            out["items"] = matched_items

    if scope in ("all", "indicators"):
        presets = match_popular_indicators(text) if text else POPULAR_INDICATORS
        if presets or scope == "indicators":
            out["indicators"] = [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "stat_code": p["stat_code"],
                    "cycle": p["cycle"],
                    "unit": p["unit"],
                }
                for p in presets
            ]

    if scope in ("all", "tables"):
        if text:
            res = client.search_statistic_tables(
                keyword=text, searchable_only=searchable_only, limit=limit, parent_code=parent_code
            )
        else:
            res = client.browse_statistic_tables(parent_code=parent_code)
            if parent_code and res["parent"] is None:
                raise ToolError(f"통계표 인덱스에 '{parent_code}' 코드가 없습니다.")
            if res["parent"]:
                out["parent"] = _compact_rows([res["parent"]])[0]
        out["tables"] = {
            "total_matches": res["total_matches"],
            "index_generated_at": res.get("index_generated_at"),
            "data": _compact_rows(
                [{k: t.get(k) for k in ("STAT_CODE", "STAT_NAME", "CYCLE", "SRCH_YN", "P_STAT_CODE")} for t in res["rows"]]
            ),
        }

    if scope == "key_statistics":
        res = await _call_ecos(client.get_all_key_statistics(language=language))
        rows = res["rows"]
        if text:
            needle = text.replace(" ", "").lower()
            rows = [
                r for r in rows
                if needle in f"{r.get('CLASS_NAME', '')}{r.get('KEYSTAT_NAME', '')}".replace(" ", "").lower()
            ]
        out["key_statistics"] = [
            {
                "class": r.get("CLASS_NAME"),
                "name": r.get("KEYSTAT_NAME"),
                "value": to_number(r.get("DATA_VALUE")),
                "unit": r.get("UNIT_NAME"),
                "time": r.get("CYCLE"),
            }
            for r in rows
        ]
        if not res["complete"]:
            out["note"] = f"전체 {res['total_count']}개 중 {len(res['rows'])}개만 조회했습니다."
    return dumps(out)


# ── Tool 2: 메타데이터 (SDMX 구조) ─────────────────────────────────

@mcp.tool(title="통계표 구조(SDMX) 조회", annotations=REMOTE_READ_ONLY, structured_output=False)
async def get_metadata(
    ctx: Context,
    stat_code: str,
    item_keyword: str | None = None,
    codes_limit: int = 50,
    output_format: str = "compact",
    language: str = "kr",
) -> str:
    """통계표의 구조를 SDMX 정보모델(Dataflow·DSD·Codelist·Concept)로 조회합니다.

    get_data에 넣을 항목코드(item_code1~4), 사용 가능한 주기, 항목별 수록기간·단위를 확인하는 단계입니다.
    - 차원(dimension): FREQ(주기) + ITEM_CODE1~4(ECOS 항목 그룹). 시계열 키는 FREQ.ITEM_CODE1.ITEM_CODE2...
    - 측정값 OBS_VALUE, 속성 UNIT_MEASURE(단위), 시간차원 TIME_PERIOD

    Args:
        stat_code: 통계표코드 (예: "901Y009"). search_statistics로 찾을 수 있습니다.
        item_keyword: 항목 이름/코드 필터 (예: "쌀", "미국달러") — 항목이 많은 표에서 유용
        codes_limit: 차원별 최대 항목 수 (기본값 50)
        output_format: "compact"(기본값, LLM용 요약) 또는 "sdmx"(SDMX-JSON 2.1 structure message)
        language: "kr" 또는 "en"

    Returns:
        통계표 구조. compact에는 바로 쓸 수 있는 get_data_example이 포함됩니다.
    """
    fmt = _choice(output_format, {"compact", "sdmx"}, "output_format", "compact")
    client = _get_client(ctx)
    code = stat_code.strip()
    info = client.table_info(code)
    if info and info.get("SRCH_YN") == "N":
        raise ToolError(
            f"'{code}'({info.get('STAT_NAME')})는 분류 항목입니다. "
            f"search_statistics(scope='tables', parent_code='{code}')로 하위 통계표를 확인하세요."
        )
    items = await _call_ecos(client.list_all_statistic_items(code, language=language))
    if not items["rows"]:
        raise ToolError(f"통계표 '{code}'의 항목을 찾을 수 없습니다. search_statistics로 코드를 확인하세요.")
    stat_name = (info or {}).get("STAT_NAME") or items["rows"][0].get("STAT_NAME") or code
    structure = sdmx.build_table_structure(code, stat_name, items["rows"], complete=items["complete"])
    structure = sdmx.filter_structure(structure, item_keyword, max(1, codes_limit))
    if fmt == "sdmx":
        return dumps(sdmx.structure_message(structure, language))
    return dumps(sdmx.compact_structure(structure, language))


# ── Tool 3: 데이터 조회 ────────────────────────────────────────────

@mcp.tool(title="시계열 데이터 조회", annotations=REMOTE_READ_ONLY, structured_output=False)
async def get_data(
    ctx: Context,
    indicator: str | None = None,
    stat_code: str | None = None,
    cycle: str | None = None,
    item_code1: str | None = None,
    item_code2: str | None = None,
    item_code3: str | None = None,
    item_code4: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int | None = None,
    transform: str | None = None,
    changes_only: bool | None = None,
    unit_mult: int | None = None,
    rebase_period: str | None = None,
    output_format: str = "compact",
    prefer_latest: bool = True,
    start_count: int = 1,
    end_count: int = DEFAULT_TIMESERIES_ROWS,
    language: str = "kr",
) -> str:
    """시계열 데이터를 조회합니다. indicator(표준 개념/인기 지표) 또는 stat_code(+항목코드) 중 하나를 지정합니다.

    ★ 표준 개념 및 인기 지표 1-shot 조회 (코드 검색 불필요):
    - 정책금리(BOK_BASE_RATE, 변경시점만), 물가상승률(CPI_INFLATION_RATE, YoY %), 소비자물가(CPI_HEADLINE, 2020=100),
      경제성장률(GDP_REAL_GROWTH, 실질 전기대비 %), 실질GDP(GDP_REAL), 명목GDP(GDP_NOMINAL),
      원/달러 환율(EXR_USD_KRW_DAILY), 월평균환율, 엔화(EXR_JPY_KRW_DAILY), 유로(EXR_EUR_KRW_DAILY),
      국고채3년(BOND_KTB_3Y_DAILY), 국고채10년, 광의통화 M2(MONEY_M2), 본원통화, 코스피(STOCK_KOSPI),
      생산자물가(PPI_HEADLINE), 경상수지, 상품수지, 외환보유액, 소비자심리지수(CCSI), 실업률 등
    - 세부 품목 1-shot (예: indicator="쌀" → 소비자물가 쌀 즉시 조회)

    ★ 데이터 정규화 및 맞춤 (Harmonization):
    - unit_mult: 단위 배수 지정 (예: 십억원(9)을 원(0)으로 변환하려면 unit_mult=0)
    - rebase_period: 지수 기준시점 리베이싱 (예: rebase_period="202401" → 해당 시점을 100으로 재산출)

    ★ 근거 블록 (Evidence Block):
    - 모든 응답에 출처(한국은행 ECOS), 통계표코드, 시계열 키, 단위, 배수, 조회시각, 원문 URL, 인용문(citation)이 자동 포함됩니다.

    Args:
        indicator: 표준 개념 ID, 키워드, 세부 품목명 (예: "기준금리", "inflation", "BOK_BASE_RATE", "쌀")
        stat_code: 통계표코드 (indicator 대신). get_metadata로 항목코드·주기 확인 권장
        cycle: 주기 A/S/Q/M/SM/D (stat_code 사용 시, 생략하면 통계표 기본 주기)
        item_code1~4: 항목코드 (생략 시 전체 항목)
        start_date: 시작 시점 (예: "2024", "2024-03", "2024Q1", "20240315")
        end_date: 종료 시점
        recent_years: 날짜 생략 시 최근 몇 년치를 조회할지
        transform: "yoy" | "pop" | "none" (indicator 사용 시 생략하면 지표 기본값)
        changes_only: 값이 바뀐 시점만 반환 (indicator 사용 시 생략하면 지표 기본값)
        unit_mult: 목표 단위 배수 (예: 0=원, 6=백만원, 9=십억원)
        rebase_period: 지수 기준 시점 (예: "202401"을 100으로 리베이싱)
        output_format: "compact"(기본값, 계열별 [시점, 값]) · "csv" · "json" · "sdmx"
        prefer_latest: 결과가 잘릴 때 최신 구간 우선 (기본값 True)
        start_count: 조회 시작 순번 (기본값 1)
        end_count: 조회 끝 순번 (기본값 1000)
        language: "kr" 또는 "en"

    Returns:
        시계열 데이터 (지정 포맷) 및 출처 근거 블록
    """
    fmt = _choice(output_format, DATA_FORMATS, "output_format", "compact")
    client = _get_client(ctx)
    series = await _resolve_series(
        client,
        indicator=indicator,
        stat_code=stat_code,
        cycle=cycle,
        item_codes=[item_code1, item_code2, item_code3, item_code4],
        transform=transform,
        changes_only=changes_only,
    )
    start, end = _date_range(series.cycle, start_date, end_date, recent_years)
    res = await _load_series(
        client,
        series,
        start,
        end,
        language=language,
        start_count=start_count,
        end_count=end_count,
        prefer_latest=prefer_latest,
    )

    table_meta = client.table_info(series.stat_code) or {}
    table_name = series.meta.get("stat_name") or table_meta.get("STAT_NAME") or series.stat_code
    item_str = "/".join(c for c in series.item_codes if c)
    series_key_str = f"{series.cycle}.{item_str}" if item_str else series.cycle
    first_row = res["rows"][0] if res.get("rows") else {}
    unit_str = first_row.get("UNIT_NAME", "")
    curr_mult = unit_multiplier_from_name(unit_str)

    # Unit multiplier conversion if requested
    if unit_mult is not None and curr_mult != unit_mult and res.get("rows"):
        scale_factor = 10 ** (curr_mult - unit_mult)
        for r in res["rows"]:
            val = to_number(r.get("DATA_VALUE"))
            if isinstance(val, (int, float)):
                r["DATA_VALUE"] = round(val * scale_factor, 6)

    # Re-basing if requested
    if rebase_period and res.get("rows"):
        for grp in group_series(res["rows"]):
            base_row = next((r for r in grp if str(r.get("TIME", "")).startswith(rebase_period)), None)
            if not base_row:
                base_row = grp[0]
            base_val = to_number(base_row.get("DATA_VALUE"))
            if isinstance(base_val, (int, float)) and base_val != 0:
                for r in grp:
                    val = to_number(r.get("DATA_VALUE"))
                    if isinstance(val, (int, float)):
                        r["DATA_VALUE"] = round((val / base_val) * 100.0, 4)

    now_kst = datetime.now(KST)
    evidence = {
        "source_agency": "한국은행 (Bank of Korea)",
        "system": "경제통계시스템 (ECOS - Economic Statistics System)",
        "table_code": series.stat_code,
        "table_name": table_name,
        "series_key": series_key_str,
        "period": f"{start} ~ {end}",
        "unit": unit_str if unit_mult is None else f"{unit_str} (scale: 10^{unit_mult - curr_mult})",
        "unit_mult": unit_mult if unit_mult is not None else curr_mult,
        "retrieved_at": now_kst.isoformat(timespec="seconds"),
        "ecos_url": f"https://ecos.bok.or.kr/#/Search/{series.stat_code}",
        "citation": f"출처: 한국은행 경제통계시스템(ECOS), '{table_name}' ({series.stat_code}, 계열키: {series_key_str}), 조회 시각: {now_kst.strftime('%Y-%m-%d %H:%M KST')}",
    }
    concept_id = series.meta.get("concept_id") or series.meta.get("sdmx_concept")
    if not concept_id:
        first_code = series.item_codes[0] if series.item_codes else None
        c_match = next(
            (
                c
                for c in CONCEPTS
                if c.ecos.stat_code == series.stat_code
                and (not c.ecos.item_code1 or c.ecos.item_code1 == first_code)
            ),
            None,
        )
        if c_match:
            concept_id = c_match.concept_id
    if concept_id:
        evidence["sdmx_concept"] = concept_id

    if fmt == "sdmx":
        notes = [res.get("note"), evidence["citation"]]
        return dumps(
            sdmx.data_message(
                res["rows"],
                stat_code=series.stat_code,
                cycle=series.cycle,
                transform=series.transform,
                notes=[n for n in notes if n],
                language=language,
            )
        )
    return format_timeseries(
        res, fmt, series.transform, meta=_series_summary(series, start, end), evidence=evidence
    )


# ── Tool 4: 시계열 비교 ────────────────────────────────────────────

@mcp.tool(title="시계열 비교·상관분석", annotations=REMOTE_READ_ONLY, structured_output=False)
async def compare_series(
    ctx: Context,
    series: list[SeriesSpec],
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int = 3,
    frequency: str | None = None,
    aggregation: str = "mean",
    transform: str | None = None,
    normalize_method: str = "none",
    rebase_period: str | None = None,
    harmonize_units: bool = False,
    join: str = "inner",
    output_format: str = "compact",
    language: str = "kr",
) -> str:
    """여러 시계열(2~6개)을 같은 주기로 맞춰 나란히 비교하고 상관계수를 계산합니다.

    예: 기준금리와 물가상승률, 원/달러 환율과 국고채 금리, CPI와 KOSPI의 관계.
    - 주기가 다르면 가장 낮은 빈도(예: 일별+월별 → 월별)로 aggregation 방식에 따라 집계합니다.
    - 날짜는 "2020", "2020-01", "2020Q1" 등 어떤 형식이든 각 계열의 주기로 변환됩니다.
    - normalize_method="rebase"와 rebase_period="202401"로 기준연도가 다른 지수들을 특정 시점(100)으로 맞춰 비교 가능합니다.
    - harmonize_units=True로 단위 배수(십억원 vs 원)를 자동 정규화할 수 있습니다.
    - 응답에 각 계열의 출처와 인용 정보를 담은 evidence 블록이 포함됩니다.

    Args:
        series: 비교할 계열 목록. 각 항목은 {"indicator": "기준금리"} 또는
            {"stat_code": "731Y004", "cycle": "M", "item_code1": "0000001", "item_code2": "0000100", "label": "원/달러"}
        start_date: 시작 시점 (생략 시 최근 recent_years년)
        end_date: 종료 시점 (생략 시 현재)
        recent_years: 날짜 생략 시 기간 (기본값 3)
        frequency: 비교 주기 A/S/Q/M/SM/D (생략 시 계열 중 가장 낮은 빈도). 더 높은 빈도로는 변환 불가
        aggregation: 주기 변환 시 집계 — "mean"(기본값) | "last" | "first" | "sum"
        transform: 모든 계열에 적용할 기본 증감률 변환 "yoy" | "pop" (계열별 transform이 우선)
        normalize_method: "none"(기본값) | "index"(첫 공통 시점=100) | "zscore" | "rebase"
        rebase_period: normalize_method="rebase" 사용 시 100으로 맞출 기준 시점 (예: "202401")
        harmonize_units: 통화/수량 단위 배수 자동 일치 여부 (기본값 False)
        join: "inner"(모든 계열에 값이 있는 시점만, 기본값) | "outer"
        output_format: "compact" | "csv"
        language: "kr" 또는 "en"

    Returns:
        정렬된 비교표(columns/rows), 쌍별 상관계수(r, n), 계열 정보 및 출처 근거 블록
    """
    if not 2 <= len(series) <= 6:
        raise ToolError("series는 2~6개를 지정하세요.")
    how = _choice(aggregation, AGGREGATIONS, "aggregation", "mean")
    norm = _choice(normalize_method, NORMALIZATIONS, "normalize_method", "none")
    join_how = _choice(join, {"inner", "outer"}, "join", "inner")
    fmt = _choice(output_format, {"compact", "csv"}, "output_format", "compact")
    client = _get_client(ctx)

    resolved = [
        await _resolve_series(
            client,
            indicator=spec.indicator,
            stat_code=spec.stat_code,
            cycle=spec.cycle,
            item_codes=[spec.item_code1, spec.item_code2, spec.item_code3, spec.item_code4],
            transform=spec.transform if spec.transform is not None else transform,
            changes_only=False,  # comparisons need every observation
            label=spec.label,
        )
        for spec in series
    ]

    target = _normalize_cycle(frequency) if frequency else coarsest_cycle([r.cycle for r in resolved])
    for r in resolved:
        if not can_convert(r.cycle, target):
            raise ToolError(
                f"'{r.label or r.stat_code}'의 주기 {r.cycle}를 더 높은 빈도 {target}로 변환할 수 없습니다. "
                "frequency를 생략하거나 더 낮은 빈도를 지정하세요."
            )

    # One date window, expressed in the target frequency, then mapped onto each series' cycle.
    window_start, window_end = _date_range(target, start_date, end_date, recent_years)

    async def load(r: ResolvedSeries) -> dict[str, Any]:
        first = to_cycle(window_start, r.cycle, "start")
        last = to_cycle(window_end, r.cycle, "end")
        return await _load_series(client, r, first, last, language=language)

    results = await asyncio.gather(*(load(r) for r in resolved))

    labels: list[str] = []
    converted: list[list[tuple[str, float]]] = []
    info: list[dict[str, Any]] = []
    notes: list[str] = []
    for r, res in zip(resolved, results):
        groups = group_series(res["rows"])
        if len(groups) > 1:
            names = ", ".join(series_label(g[0]) for g in groups[:5])
            raise ToolError(
                f"'{r.label or r.stat_code}'가 여러 계열({len(groups)}개: {names} ...)을 반환합니다. "
                "item_code를 지정해 계열 하나로 좁혀 주세요 (get_metadata로 확인)."
            )
        rows = groups[0] if groups else []
        value_field = TRANSFORMS[r.transform] if r.transform else "DATA_VALUE"
        unit_str = rows[0].get("UNIT_NAME") if rows else ""
        points = [
            (str(row["TIME"]), value)
            for row in rows
            if isinstance(value := to_number(row.get(value_field)), int | float)
        ]

        if harmonize_units and not r.transform:
            mult = unit_multiplier_from_name(unit_str)
            if mult != 0:
                points = scale_multiplier(points, mult, 0)
                unit_str = "원" if "원" in (unit_str or "") else "USD"

        points = normalize(
            convert_frequency(points, r.cycle, target, how),
            norm,
            base_period=rebase_period,
        )

        label = r.label or (series_label(rows[0]) if rows else r.stat_code)
        while label in labels:
            label += "'"
        labels.append(label)
        converted.append(points)
        info.append(
            {
                "label": label,
                "stat_code": r.stat_code,
                "item_code": "/".join(c for c in series_key(rows[0]) if c) if rows else None,
                "unit": "%" if r.transform else unit_str,
                "source_cycle": r.cycle,
                **({"transform": r.transform} if r.transform else {}),
                "points": len(points),
            }
        )
        if res.get("note"):
            notes.append(f"{label}: {res['note']}")

    table = align(converted, join_how)
    correlation = correlation_matrix(labels, table)

    evidence_list = []
    now_kst = datetime.now(KST)
    for r, inf in zip(resolved, info):
        t_info = client.table_info(r.stat_code) or {}
        t_name = r.meta.get("stat_name") or t_info.get("STAT_NAME") or r.stat_code
        evidence_list.append(
            {
                "label": inf["label"],
                "source_agency": "한국은행 (Bank of Korea)",
                "table_code": r.stat_code,
                "table_name": t_name,
                "series_key": f"{r.cycle}.{inf.get('item_code') or ''}",
                "unit": inf.get("unit"),
                "retrieved_at": now_kst.isoformat(timespec="seconds"),
                "ecos_url": f"https://ecos.bok.or.kr/#/Search/{r.stat_code}",
                "citation": f"출처: 한국은행 경제통계시스템(ECOS), '{t_name}' ({r.stat_code})",
            }
        )

    summary: dict[str, Any] = {
        "frequency": target,
        "aggregation": how,
        "start_date": window_start,
        "end_date": window_end,
        **({"normalize": norm} if norm != "none" else {}),
        **({"rebase_period": rebase_period} if rebase_period else {}),
        "series": [{k: v for k, v in i.items() if v is not None} for i in info],
        "correlation": correlation,
        "evidence": evidence_list,
    }
    if notes:
        summary["notes"] = notes

    if fmt == "csv":
        buffer = io.StringIO()
        buffer.write(f"# frequency: {target}, aggregation: {how}\n")
        for ev in evidence_list:
            buffer.write(f"# evidence {ev['label']}: {ev['citation']} | {ev['ecos_url']}\n")
        for pair, stats in correlation.items():
            buffer.write(f"# corr {pair}: r={stats['r']} (n={stats['n']})\n")
        for note in notes:
            buffer.write(f"# note: {note}\n")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["TIME", *labels])
        writer.writerows(["" if v is None else v for v in row] for row in table)
        return buffer.getvalue().rstrip("\n")
    return dumps({**summary, "columns": ["time", *labels], "rows": table})


# ── Tool 5: 통계 계산 ──────────────────────────────────────────────

@mcp.tool(title="기술통계·추세 계산", annotations=REMOTE_READ_ONLY, structured_output=False)
async def calculate_statistics(
    ctx: Context,
    indicator: str | None = None,
    stat_code: str | None = None,
    cycle: str | None = None,
    item_code1: str | None = None,
    item_code2: str | None = None,
    item_code3: str | None = None,
    item_code4: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int | None = None,
    language: str = "kr",
) -> str:
    """시계열의 요약통계를 계산합니다. 원자료를 모두 읽지 않고도 추이를 파악할 때 사용합니다.

    계열별로 반환: count, first/last, min/max(시점 포함), mean, median, std, change/change_pct(기간 변화),
    cagr_pct(연평균 증가율), trend_per_year/trend_r2(선형추세), latest_pop_pct·pop_pct_std(전기비와 변동성),
    max_drawdown_pct(고점 대비 최대 하락률), latest_yoy_pct·mean_yoy_pct(전년동기비, 일별 제외).

    Args:
        indicator: 인기 지표 키워드 또는 id (예: "환율", "물가")
        stat_code: 통계표코드 (indicator 대신)
        cycle: 주기 (stat_code 사용 시)
        item_code1~4: 항목코드
        start_date: 시작 시점 (생략 시 일간은 최근 3개월, 그 외 2년)
        end_date: 종료 시점
        recent_years: 날짜 생략 시 기간
        language: "kr" 또는 "en"

    Returns:
        계열별 요약통계
    """
    client = _get_client(ctx)
    series = await _resolve_series(
        client,
        indicator=indicator,
        stat_code=stat_code,
        cycle=cycle,
        item_codes=[item_code1, item_code2, item_code3, item_code4],
        transform="none",
        changes_only=False,
    )
    series.transform = "yoy" if series.cycle != "D" else None
    start, end = _date_range(series.cycle, start_date, end_date, recent_years)
    res = await _load_series(client, series, start, end, language=language, apply_changes_only=False)

    stats = []
    for rows in group_series(res["rows"]):
        points = [
            (str(r["TIME"]), value)
            for r in rows
            if isinstance(value := to_number(r.get("DATA_VALUE")), int | float)
        ]
        yoy = [(str(r["TIME"]), r.get("yoy_pct")) for r in rows] if series.transform else None
        stats.append(
            {
                "item": series_label(rows[0]),
                "item_code": "/".join(c for c in series_key(rows[0]) if c),
                "unit": rows[0].get("UNIT_NAME"),
                "stats": describe(series.cycle, points, yoy),
            }
        )
    first_row = res["rows"][0] if res["rows"] else {}
    series.transform = None
    table_meta = client.table_info(series.stat_code) or {}
    table_name = (
        series.meta.get("stat_name")
        or first_row.get("STAT_NAME")
        or table_meta.get("STAT_NAME")
        or series.stat_code
    )
    series_item_str = "/".join(c for c in series.item_codes if c)
    series_key_str = f"{series.cycle}.{series_item_str}" if series_item_str else series.cycle
    now_kst = datetime.now(KST)
    evidence = {
        "source_agency": "한국은행 (Bank of Korea)",
        "system": "경제통계시스템 (ECOS - Economic Statistics System)",
        "table_code": series.stat_code,
        "table_name": table_name,
        "series_key": series_key_str,
        "period": f"{start} ~ {end}",
        "retrieved_at": now_kst.isoformat(timespec="seconds"),
        "ecos_url": f"https://ecos.bok.or.kr/#/Search/{series.stat_code}",
        "citation": f"출처: 한국은행 경제통계시스템(ECOS), '{table_name}' ({series.stat_code}, 계열키: {series_key_str}), 조회 시각: {now_kst.strftime('%Y-%m-%d %H:%M KST')}",
    }
    if series.meta.get("concept_id"):
        evidence["sdmx_concept"] = series.meta["concept_id"]

    out = {
        "stat_code": series.stat_code,
        "stat_name": table_name,
        **_series_summary(series, start, end),
        "evidence": evidence,
        "series": stats,
    }
    if res.get("note"):
        out["note"] = res["note"]
    if res.get("truncated"):
        out["truncated"] = True
    return dumps(out)


# ── Tool 6: 지표 설명 ──────────────────────────────────────────────

def _meta_candidates(term: str, table_name: str | None) -> list[str]:
    names = [term.strip()]
    if table_name:
        stripped = table_name.split(". ", 1)[-1] if ". " in table_name else table_name
        names.append(stripped)
        names.append(stripped.split("(")[0].strip())
    return list(dict.fromkeys(n for n in names if n))[:3]


@mcp.tool(title="지표 설명", annotations=REMOTE_READ_ONLY, structured_output=False)
async def explain_indicator(
    ctx: Context,
    term: str,
    stat_code: str | None = None,
    language: str = "kr",
) -> str:
    """경제지표·통계용어의 뜻과 작성 방법, 관련 통계표와 SDMX 구조를 한 번에 설명합니다.

    한국은행 통계용어사전(정의), 통계 설명자료(작성기관·주기·작성방법 등), 표준 개념 체계(SDMX 및 국제기구 매핑),
    관련 인기 지표 프리셋과 통계표, 그리고 통계표의 SDMX 개념(차원·단위)을 모아 반환합니다.

    Args:
        term: 지표·용어 (예: "소비자물가지수", "경제심리지수", "기준금리", "M2", "inflation")
        stat_code: 특정 통계표를 설명하려면 통계표코드 지정 (생략 시 term과 가장 관련 있는 것 사용)
        language: "kr" 또는 "en"

    Returns:
        definition(용어 정의), methodology(통계 설명자료), canonical_concept(SDMX 표준 개념), indicators, tables, sdmx(구조 요약)
    """
    client = _get_client(ctx)
    term = term.strip()
    if not term:
        raise ToolError("term을 입력하세요.")

    presets = match_popular_indicators(term)[:3]
    concept_hits = search_concepts(term, limit=1)
    tables = client.search_statistic_tables(term, searchable_only=True, limit=5)["rows"]
    code = (stat_code or "").strip() or (presets[0]["stat_code"] if len(presets) == 1 else None)
    if not code and concept_hits:
        code = concept_hits[0]["ecos"]["stat_code"]
    if not code and tables:
        code = tables[0]["STAT_CODE"]
    info = client.table_info(code) if code else None

    async def glossary() -> list[dict[str, Any]]:
        candidates = [term] + [p["name"].split("(")[0] for p in presets]
        for word in dict.fromkeys(candidates):
            try:
                res = await client.search_statistic_word(word, language=language, end_count=3)
            except EcosApiError:
                continue
            if res["rows"]:
                return [{"word": r.get("WORD"), "definition": r.get("CONTENT")} for r in res["rows"]]
        return []

    async def methodology() -> list[dict[str, Any]]:
        for name in _meta_candidates(term, (info or {}).get("STAT_NAME")):
            try:
                res = await client.get_all_statistic_meta(name, language=language)
            except EcosApiError:
                continue
            entries = [
                {"section": r.get("CONT_NAME"), "text": str(r["META_DATA"])[:600]}
                for r in res["rows"]
                if r.get("META_DATA")
            ]
            if entries:
                return [{"dataset": name}, *entries[:10]]
        return []

    async def structure() -> dict[str, Any] | None:
        if not code:
            return None
        try:
            items = await client.list_all_statistic_items(code, language=language)
        except EcosApiError:
            return None
        if not items["rows"]:
            return None
        name = (info or {}).get("STAT_NAME") or items["rows"][0].get("STAT_NAME") or code
        built = sdmx.build_table_structure(code, name, items["rows"], complete=items["complete"])
        return {
            "dataflow": {"id": code, "name": name, "urn": sdmx.dataflow_urn(code)},
            "frequencies": built.cycles,
            "series_key": built.series_key,
            "dimensions": [
                {"id": d.id, "concept": d.concept_name, "total_codes": d.total_codes}
                for d in built.dimensions
            ],
            "units": sorted({c.unit for d in built.dimensions for c in d.codes if c.unit})[:10],
        }

    definition, methods, sdmx_view = await asyncio.gather(glossary(), methodology(), structure())
    out: dict[str, Any] = {"term": term}
    if definition:
        out["definition"] = definition
    if methods:
        out["methodology"] = methods
    if concept_hits:
        out["canonical_concept"] = concept_hits[0]
    if presets:
        out["indicators"] = [
            {"id": p["id"], "name": p["name"], "stat_code": p["stat_code"], "cycle": p["cycle"], "unit": p["unit"]}
            for p in presets
        ]
    if tables:
        out["tables"] = _compact_rows(
            [{k: t.get(k) for k in ("STAT_CODE", "STAT_NAME", "CYCLE")} for t in tables]
        )
    if sdmx_view:
        out["sdmx"] = sdmx_view
    if len(out) == 1:
        raise ToolError(f"'{term}'에 대한 정보를 찾지 못했습니다. search_statistics로 다른 검색어를 시도하세요.")
    return dumps(out)


# ── MCP Resources ───────────────────────────────────────────────────

@mcp.resource("ecos://popular-indicators")
def get_popular_indicators_resource() -> str:
    """한국은행 주요 핵심 경제지표 코드 및 주기 매핑표 리소스."""
    return dumps(
        {
            "description": "get_data(indicator=...)로 바로 조회되는 주요 경제지표 코드 및 주기 매핑표",
            "indicators": POPULAR_INDICATORS,
        }
    )


@mcp.resource("ecos://canonical-concepts")
def get_canonical_concepts_resource() -> str:
    """SDMX 표준 경제 개념 체계 및 ECOS·국제기구 매핑표 리소스."""
    return dumps(
        {
            "description": "SDMX 표준 경제 개념(BOK_BASE_RATE, CPI_HEADLINE 등) 및 IMF/OECD/BIS 연동 매핑표",
            "concepts": all_concepts(),
        }
    )


@mcp.resource("ecos://date-format-guide")
def get_date_format_guide_resource() -> str:
    """주기별 올바른 날짜 포맷 규격서 리소스."""
    return dumps(
        {
            "description": "ECOS API 주기(Cycle)별 검색일자 규격. '2024', '2024-03', '2024Q1' 등은 자동 변환됩니다.",
            "cycles": CYCLE_DESCRIPTIONS,
        }
    )


@mcp.resource("ecos://sdmx/conventions", mime_type="text/plain")
def get_sdmx_conventions_resource() -> str:
    """ECOS → SDMX 매핑 규칙 (Dataflow/DSD/Codelist/Concept, 시점 표기, 코드 이스케이프)."""
    return sdmx.__doc__ or ""


# ── MCP Prompts ─────────────────────────────────────────────────────

@mcp.prompt(name="macro-economic-briefing")
def macro_economic_briefing() -> str:
    """한국 거시경제 핵심 지표 종합 브리핑 프롬프트."""
    return (
        "한국 거시경제의 현재 상황을 종합적으로 분석하고 브리핑해주세요.\n"
        "다음 절차로 진행해주세요:\n"
        "1. search_statistics(scope='key_statistics')로 100대 주요 경제지표의 최신값을 확인합니다.\n"
        "2. calculate_statistics로 '성장률', '물가상승률', '기준금리', '월평균환율'의 최근 2년 추이를 요약합니다.\n"
        "3. compare_series로 기준금리와 물가상승률의 관계를 확인합니다.\n"
        "4. 모든 수치와 분석에는 응답에 포함된 evidence(출처: 한국은행 ECOS, 통계표, 시계열 키, 조회시각)를 명시하여 인용하세요.\n"
        "5. 한국 경제의 현 위치, 주요 리스크 요인, 향후 경기 전망을 전문 애널리스트 관점에서 보고서 형태로 작성해주세요."
    )


@mcp.prompt(name="analyze-economic-trend")
def analyze_economic_trend(indicator_name: str = "소비자물가지수") -> str:
    """특정 경제 지표의 시계열 추이 및 시사점 심층 분석 프롬프트."""
    return (
        f"'{indicator_name}' 지표의 최근 시계열 추이를 분석해주세요.\n"
        "다음 절차로 진행해주세요:\n"
        f"1. explain_indicator(term='{indicator_name}')로 지표의 정의·작성방법, SDMX 개념, 통계표를 확인합니다.\n"
        "2. get_data로 최근 3년 데이터를 조회합니다 (표준 개념 ID나 세부 품목명도 지원). "
        "수준값 지표라면 transform='yoy'도 확인합니다.\n"
        "3. calculate_statistics로 변화율·추세·변동성을 계산합니다.\n"
        "4. 분석 내용에 통계 출처, 시계열 키, 조회 시각(evidence)을 명시하여 신뢰성 있는 답변을 작성하세요.\n"
        "5. 수치 추이, 주요 변곡점과 그 배경 요인, 정책적 시사점을 체계적으로 분석하여 설명해주세요."
    )


# ── CLI Health Check ────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 12 else "****"


async def run_health_check() -> int:
    """Run interactive diagnostics check for CLI users."""
    print("=" * 65)
    print(f"🩺 ECOS MCP Server {__version__} — 자가 진단 및 헬스체크")
    print("=" * 65)

    # 1. Environment & Python
    py_ver = sys.version.split()[0]
    print(f"\n[1/4] 실행 환경: Python {py_ver} ({sys.platform})")
    print("  ✅ Python 버전 정상 (>=3.11)")

    # 2. API Key
    print("\n[2/4] ECOS API Key 구성 확인...")
    if ECOS_API_KEY == SAMPLE_API_KEY:
        print("  ⚠️ 현재 'sample' 키를 사용 중입니다. (1회 최대 10건 조회 제한)")
        print("     정식 키 발급: https://ecos.bok.or.kr/api/#/ (무료)")
    else:
        print(f"  ✅ 사용자 API 키 설정됨 ({_mask_key(ECOS_API_KEY)})")

    # 3. Network & API test
    print("\n[3/4] 한국은행 ECOS 서버 네트워크 통신 확인...")
    client = EcosClient()
    try:
        try:
            await client.get_key_statistics(start_count=1, end_count=1)
            print("  ✅ 한국은행 ECOS API 연결 정상 (100대 지표 응답 수신 성공)")
        except EcosApiError as e:
            print(f"  ❌ 연결 실패: {e}")
            return 1

        # 4. Table Index Check
        print("\n[4/4] 통계표 로컬 검색 인덱스 검사...")
        search_res = client.search_statistic_tables("물가", limit=3)
        table_count = len(client._load_tables_cache())
        if search_res.get("total_matches", 0) > 0:
            print(
                f"  ✅ 통계표 인덱스 로드 성공 ({table_count}개, 생성일: {client.tables_generated_at or '미상'}, "
                f"'물가' 검색 결과: {search_res['total_matches']}건)"
            )
        else:
            print("  ❌ 통계표 인덱스를 불러올 수 없습니다.")
            return 1
    finally:
        await client.close()

    print("\n" + "=" * 65)
    print("🎉 모든 진단 검사를 통과했습니다! ECOS MCP 서버를 실행할 준비가 되었습니다.")
    print("   MCP 클라이언트 설정에 'uvx ecos-mcp'를 등록하세요. (README 참고)")
    print("=" * 65)
    return 0


# ── Entry point ─────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    """Run the ECOS MCP server (stdio) or the CLI health check."""
    parser = argparse.ArgumentParser(
        prog="ecos-mcp",
        description="한국은행 ECOS Open API MCP 서버. 옵션 없이 실행하면 stdio MCP 서버로 동작합니다.",
        epilog="API 키는 ECOS_API_KEY 환경변수로 설정합니다 (미설정 시 sample 키).",
    )
    parser.add_argument(
        "--check",
        "-c",
        "--health",
        "--test",
        action="store_true",
        help="API 키, ECOS 서버 연결, 통계표 인덱스를 진단하고 종료합니다",
    )
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.check:
        sys.exit(asyncio.run(run_health_check()))
    mcp.run()


if __name__ == "__main__":
    main()
