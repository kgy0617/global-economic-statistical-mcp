"""Global Economic Statistical MCP server.

    LLM ── 6 MCP tools ── Concept Resolver ── Concept Catalog / Provider Catalog
                                   │
                           Provider Resolver
                 ┌─────────────────┴──────────────────┐
               ECOS (Bank of Korea REST)     SDMX: OECD · IMF · BIS
                 └─────────────────┬──────────────────┘
                           Canonical Model → Validation → Analysis → Provenance

Tools:
- search_statistics: concepts, ECOS items/tables, OECD·IMF·BIS dataflows, ECOS key statistics
- get_metadata: structure of an ECOS table (SDMX-mapped) or an OECD·IMF·BIS dataflow (DSD)
- get_data: one concept for a country (or a direct ECOS/SDMX query), validated, with provenance
- compare_series: several series/countries/sources aligned, correlated and cross-validated
- calculate_statistics: descriptive statistics, growth, trend, volatility
- explain_indicator: concept definition, available sources, ECOS glossary and methodology
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from global_economic_statistical_mcp import ecos_sdmx
from global_economic_statistical_mcp.analytics import (
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
)
from global_economic_statistical_mcp.catalog.concepts import (
    CONCEPTS,
    find_concept,
    search_concepts,
)
from global_economic_statistical_mcp.catalog.countries import all_countries
from global_economic_statistical_mcp.catalog.search import (
    dataflow_name,
    search_dataflows,
    search_items,
)
from global_economic_statistical_mcp.config import (
    CYCLE_DATE_FORMATS,
    CYCLE_DESCRIPTIONS,
    ECOS_API_KEY,
    PERIODS_PER_YEAR,
    SAMPLE_API_KEY,
    VALID_CYCLES,
    get_default_date_range,
    period_to_index,
    to_cycle,
)
from global_economic_statistical_mcp.ecos_client import EcosApiError, EcosClient
from global_economic_statistical_mcp.formatting import FORMATS, render
from global_economic_statistical_mcp.model import ecos_to_canonical, to_ecos_period
from global_economic_statistical_mcp.providers.base import ProviderError
from global_economic_statistical_mcp.providers.sdmx_rest import SOURCES
from global_economic_statistical_mcp.service import (
    LoadedSeries,
    ResolutionError,
    ResolvedSource,
    StatService,
)
from global_economic_statistical_mcp.storage import ValidationLedger
from global_economic_statistical_mcp.timeseries import dumps, to_number
from global_economic_statistical_mcp.validation import cross_validate

try:
    __version__ = version("global-economic-statistical-mcp")
except PackageNotFoundError:  # imported from a source tree without installation
    __version__ = "0.0.0+unknown"


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, Any]]:
    service = StatService()
    try:
        yield {"service": service}
    finally:
        await service.close()


mcp = MCPServer(
    name="Global Economic Statistical MCP",
    version=__version__,
    description=(
        "한국은행 ECOS와 OECD·IMF·BIS(SDMX) 거시경제 통계를 하나의 개념 체계(Concept Catalog)와 "
        "표준 시계열 모델로 조회·검증·비교하는 MCP 서버. 모든 응답에 출처(provenance)와 검증 결과가 포함됩니다."
    ),
    instructions=(
        "1) 지표는 표준 개념(concept)으로 조회하세요: get_data(indicator='CPI_YOY', country='US'). "
        "한국은 한국은행 ECOS가 1순위이고, 다른 나라는 OECD·IMF·BIS에서 가져옵니다. "
        "2) 개념이 없는 통계는 search_statistics로 ECOS 통계표/품목이나 OECD·IMF·BIS 데이터플로를 찾고 "
        "get_metadata로 키를 확인한 뒤 get_data(stat_code=...) 또는 get_data(source=..., dataflow=..., key=...)로 조회합니다. "
        "3) 응답의 validation(단위·주기·배수·기간·국가·결측·중복·개정 검사)을 확인하고, 출처가 여럿인 개념은 "
        "cross_validate=True로 기관 간 값을 대조하세요. "
        "4) 수치를 인용할 때는 provenance의 citation을 함께 제시하세요."
    ),
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def _service(ctx: Context) -> StatService:
    return ctx.request_context.lifespan_context["service"]


def _choice(value: str | None, allowed: set[str], name: str, default: str) -> str:
    choice = (value or default).strip().lower()
    if choice not in allowed:
        raise ToolError(f"지원되지 않는 {name}입니다: '{value}'. {', '.join(sorted(allowed))} 중 하나를 사용하세요.")
    return choice


def _compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if v not in (None, "")} for r in rows]


async def _guard(coro: Any) -> Any:
    """Map domain errors to MCP tool errors (messages never contain credentials)."""
    try:
        return await coro
    except (ResolutionError, ProviderError, EcosApiError) as e:
        raise ToolError(str(e)) from e


def _date_window(freq: str, start_date: str | None, end_date: str | None, recent_years: int | None) -> tuple[str, str]:
    """Defaults and flexible date input → canonical (start, end) for a frequency."""
    def_start, def_end = get_default_date_range(freq, recent_years=recent_years)
    try:
        start = to_cycle(start_date, freq, "start") if start_date else def_start
        end = to_cycle(end_date, freq, "end") if end_date else def_end
    except ValueError as e:
        raise ResolutionError(
            f"{e}. 주기 '{freq}'의 포맷은 {CYCLE_DATE_FORMATS[freq][1]}이며, '2024', '2024-03', '2024Q1', "
            "'2024-03-15' 같은 형식도 자동 변환됩니다."
        ) from e
    if period_to_index(freq, start) > period_to_index(freq, end):
        raise ResolutionError(f"start_date('{start}')가 end_date('{end}')보다 늦습니다.")
    return ecos_to_canonical(start, freq), ecos_to_canonical(end, freq)


async def _default_ecos_cycle(client: EcosClient, stat_code: str | None) -> str | None:
    if not stat_code:
        return None
    info = client.table_info(stat_code.strip())
    if info and info.get("CYCLE") in VALID_CYCLES:
        return info["CYCLE"]
    items = await client.list_statistic_items(stat_code.strip(), end_count=10)
    cycles = {r.get("CYCLE") for r in items["rows"]}
    return next((c for c in ("M", "Q", "A", "D", "S", "SM") if c in cycles), None)


class SeriesSpec(BaseModel):
    """One series: a concept for a country, an ECOS table, or an OECD/IMF/BIS dataflow."""

    indicator: str | None = Field(None, description="표준 개념 id 또는 이름 (예: 'CPI_YOY', '기준금리')")
    country: str | None = Field(None, description="국가 ISO 코드 (기본값 KR)")
    source: str | None = Field(None, description="ECOS | OECD | IMF | BIS (생략 시 우선순위대로)")
    stat_code: str | None = Field(None, description="ECOS 통계표코드 (indicator 대신)")
    cycle: str | None = Field(None, description="주기 A/S/Q/M/SM/D")
    item_code1: str | None = None
    item_code2: str | None = None
    item_code3: str | None = None
    item_code4: str | None = None
    dataflow: str | None = Field(None, description="SDMX 데이터플로 (예: 'BIS:WS_CBPOL(1.0)')")
    key: str | None = Field(None, description="SDMX 시계열 키 (예: 'M.US')")
    transform: str | None = Field(None, description="'yoy' | 'pop' | 'none'")
    label: str | None = Field(None, description="결과에 표시할 이름")


async def _resolve(service: StatService, spec: SeriesSpec, *, changes_only: bool | None = None) -> list[ResolvedSource]:
    default_cycle = await _default_ecos_cycle(service.ecos_client, spec.stat_code) if spec.stat_code and not spec.cycle else None
    return service.resolve(
        indicator=spec.indicator,
        country=spec.country,
        source=spec.source,
        freq=spec.cycle,
        stat_code=spec.stat_code,
        item_codes=[spec.item_code1, spec.item_code2, spec.item_code3, spec.item_code4],
        dataflow=spec.dataflow,
        key=spec.key,
        transform=spec.transform,
        changes_only=changes_only,
        label=spec.label,
        default_ecos_cycle=default_cycle,
    )


# ── Tool 1: search ──────────────────────────────────────────────────

@mcp.tool(title="통계 검색", annotations=READ_ONLY, structured_output=False)
async def search_statistics(
    ctx: Context,
    query: str | None = None,
    scope: str = "all",
    source: str | None = None,
    parent_code: str | None = None,
    searchable_only: bool = True,
    limit: int = 20,
    language: str = "kr",
) -> str:
    """표준 개념, ECOS 통계표·품목, OECD·IMF·BIS 데이터플로를 검색합니다. 조회 전 첫 단계입니다.

    scope:
    - "all"(기본값): concepts + ECOS items + ECOS tables + international dataflows
    - "concepts": 여러 기관을 묶은 표준 개념 (get_data(indicator=...)로 바로 조회, 국가 지정 가능)
    - "items": ECOS 세부 품목 (예: '쌀', '휘발유', '위안화') — 코드는 ECOS API에서 생성한 색인 기준
    - "tables": ECOS 통계표 (query 없으면 parent_code 분류의 하위 항목)
    - "dataflows": OECD·IMF·BIS 데이터플로 (영문 이름·id, 단어 모두 포함; source로 기관 제한)
    - "key_statistics": 한국은행 100대 주요 경제지표 최신값

    Args:
        query: 검색어 (예: "물가", "policy rate", "unemployment", "쌀")
        scope: "all" | "concepts" | "items" | "tables" | "dataflows" | "key_statistics"
        source: dataflows 검색 시 기관 제한 (OECD | IMF | BIS)
        parent_code: ECOS 통계표 분류 코드
        searchable_only: ECOS 통계표 검색 시 조회 가능한 표만
        limit: 범주별 최대 결과 수
        language: key_statistics 응답 언어

    Returns:
        범주별 검색 결과 (concepts, items, tables, dataflows, key_statistics)
    """
    scope = _choice(scope, {"all", "concepts", "items", "tables", "dataflows", "key_statistics"}, "scope", "all")
    service = _service(ctx)
    text = (query or "").strip()
    out: dict[str, Any] = {}

    if scope in ("all", "concepts"):
        out["concepts"] = [c.summary() for c in search_concepts(text, limit=limit)]
    if scope in ("all", "items") and text:
        out["items"] = search_items(text, limit=limit)
    if scope in ("all", "tables"):
        client = service.ecos_client
        if text:
            res = client.search_statistic_tables(keyword=text, searchable_only=searchable_only, limit=limit, parent_code=parent_code)
        else:
            res = client.browse_statistic_tables(parent_code=parent_code)
            if parent_code and res["parent"] is None:
                raise ToolError(f"ECOS 통계표 인덱스에 '{parent_code}' 코드가 없습니다.")
        out["tables"] = {
            "total_matches": res["total_matches"],
            "data": _compact_rows(
                [{k: t.get(k) for k in ("STAT_CODE", "STAT_NAME", "CYCLE", "SRCH_YN", "P_STAT_CODE")} for t in res["rows"]]
            ),
        }
    if scope in ("all", "dataflows") and text:
        if source and source.upper() not in SOURCES:
            raise ToolError("dataflows 검색의 source는 OECD, IMF, BIS 중 하나입니다.")
        out["dataflows"] = search_dataflows(text, provider=source, limit=limit)
    if scope == "key_statistics":
        res = await _guard(service.ecos_client.get_all_key_statistics(language=language))
        rows = res["rows"]
        if text:
            needle = text.replace(" ", "").lower()
            rows = [r for r in rows if needle in f"{r.get('CLASS_NAME', '')}{r.get('KEYSTAT_NAME', '')}".replace(" ", "").lower()]
        out["key_statistics"] = [
            {
                "class": r.get("CLASS_NAME"),
                "name": r.get("KEYSTAT_NAME"),
                "value": to_number(r.get("DATA_VALUE")),
                "unit": r.get("UNIT_NAME"),
                "time": r.get("CYCLE"),  # ECOS puts the reference period in CYCLE here
            }
            for r in rows
        ]
    return dumps(out)


# ── Tool 2: metadata ────────────────────────────────────────────────

@mcp.tool(title="구조(메타데이터) 조회", annotations=READ_ONLY, structured_output=False)
async def get_metadata(
    ctx: Context,
    stat_code: str | None = None,
    source: str | None = None,
    dataflow: str | None = None,
    code_keyword: str | None = None,
    codes_limit: int = 30,
    output_format: str = "compact",
    language: str = "kr",
) -> str:
    """통계의 구조(차원·코드목록)를 조회해 get_data에 넣을 코드를 확인합니다.

    - ECOS: stat_code 지정 → SDMX로 매핑한 구조 (FREQ + ITEM_CODE1~4, 수록기간·단위). output_format="sdmx" 가능
    - OECD·IMF·BIS: source + dataflow 지정 → 기관이 발행한 실제 DSD의 차원과 코드목록.
      시계열 키는 차원 순서대로 코드를 '.'로 이은 것입니다 (예: BIS WS_CBPOL → 'M.KR').

    Args:
        stat_code: ECOS 통계표코드 (예: "901Y009")
        source: OECD | IMF | BIS (dataflow와 함께)
        dataflow: SDMX 데이터플로 (예: "BIS:WS_CBPOL(1.0)", "IMF.STA:CPI")
        code_keyword: 코드 이름/값 필터 (예: "쌀", "Korea", "KOR")
        codes_limit: 차원별 최대 코드 수 (기본값 30)
        output_format: "compact" | "sdmx"(ECOS만)
        language: "kr" | "en"

    Returns:
        차원 목록, 차원별 코드, 키 예시
    """
    fmt = _choice(output_format, {"compact", "sdmx"}, "output_format", "compact")
    service = _service(ctx)
    if dataflow:
        provider = (source or "").upper()
        if provider not in SOURCES:
            raise ToolError("dataflow 구조 조회에는 source를 OECD, IMF, BIS 중 하나로 지정하세요.")
        if fmt == "sdmx":
            raise ToolError("SDMX 기관 데이터플로의 원본 구조는 structure_url에서 직접 받을 수 있습니다. compact를 사용하세요.")
        summary = await _guard(service.providers[provider].structure(dataflow.strip()))
        keyword = (code_keyword or "").strip().lower()
        for dim in summary["dimensions"]:
            codes = dim.pop("codes")
            dim["total_codes"] = len(codes)
            if keyword:
                matched = [c for c in codes if keyword in (c["code"] or "").lower() or keyword in (c["name"] or "").lower()]
                codes = matched or codes
            dim["codes"] = codes[: max(1, codes_limit)]
        summary["key_template"] = ".".join(f"{{{d['id']}}}" for d in summary["dimensions"])
        summary["provider"] = provider
        return dumps(summary)

    if not stat_code:
        raise ToolError("stat_code(ECOS) 또는 source+dataflow(OECD·IMF·BIS)를 지정하세요.")
    client = service.ecos_client
    code = stat_code.strip()
    info = client.table_info(code)
    if info and info.get("SRCH_YN") == "N":
        raise ToolError(
            f"'{code}'({info.get('STAT_NAME')})는 분류 항목입니다. search_statistics(scope='tables', parent_code='{code}')로 하위 통계표를 확인하세요."
        )
    items = await _guard(client.list_all_statistic_items(code, language=language))
    if not items["rows"]:
        raise ToolError(f"통계표 '{code}'의 항목을 찾을 수 없습니다. search_statistics로 코드를 확인하세요.")
    stat_name = (info or {}).get("STAT_NAME") or items["rows"][0].get("STAT_NAME") or code
    structure = ecos_sdmx.build_table_structure(code, stat_name, items["rows"], complete=items["complete"])
    structure = ecos_sdmx.filter_structure(structure, code_keyword, max(1, codes_limit))
    if fmt == "sdmx":
        return dumps(ecos_sdmx.structure_message(structure, language))
    return dumps({"provider": "ECOS", **ecos_sdmx.compact_structure(structure, language)})


# ── Tool 3: data ────────────────────────────────────────────────────

def _harmonize(loaded: LoadedSeries, rebase_period: str | None, unit_mult: int | None = None) -> None:
    if unit_mult is not None:
        for series in loaded.series:
            if series.unit_mult == unit_mult:
                continue
            factor = 10 ** (series.unit_mult - unit_mult)
            scaled = scale_multiplier([(o.period, o.value) for o in series.observations], series.unit_mult, unit_mult)
            for obs, (_, value) in zip(series.observations, scaled):
                obs.value = value
            series.provenance.transformations.append(f"scale: 10^{series.unit_mult} → 10^{unit_mult} (×{factor:g})")
            series.unit_mult = unit_mult
    if not rebase_period:
        return
    for series in loaded.series:
        if (series.unit or loaded.source.expected_unit) != "IX":
            raise ResolutionError("rebase_period는 지수(IX) 시계열에만 쓸 수 있습니다.")
        target = ecos_to_canonical(to_cycle(rebase_period, series.freq, "start"), series.freq)
        points = [(o.period, o.value) for o in series.observations]
        if target not in dict(points):
            raise ResolutionError(f"재기준 시점 {target}의 값이 없습니다 (조회 기간 안의 시점을 지정하세요).")
        for obs, (_, value) in zip(series.observations, rebase_index(points, base_period=target)):
            obs.value = value
        series.base_period = target.replace("-", "")
        series.provenance.transformations.append(f"rebase: {target}=100으로 재기준화")


@mcp.tool(title="시계열 조회(검증·출처 포함)", annotations=READ_ONLY, structured_output=False)
async def get_data(
    ctx: Context,
    indicator: str | None = None,
    country: str | None = None,
    source: str | None = None,
    stat_code: str | None = None,
    cycle: str | None = None,
    item_code1: str | None = None,
    item_code2: str | None = None,
    item_code3: str | None = None,
    item_code4: str | None = None,
    dataflow: str | None = None,
    key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int | None = None,
    transform: str | None = None,
    changes_only: bool | None = None,
    rebase_period: str | None = None,
    unit_mult: int | None = None,
    cross_validate: bool = False,
    output_format: str = "compact",
    prefer_latest: bool = True,
    start_count: int = 1,
    end_count: int = 1000,
    language: str = "kr",
) -> str:
    """시계열을 조회합니다. 모든 결과는 표준 모델로 변환되고, 검증(validation)과 출처(provenance)가 붙습니다.

    조회 방법 (셋 중 하나):
    1. 표준 개념: indicator(+country, source, cycle) — 예: indicator="CPI_YOY", country="US"
       한국은 ECOS 우선, 그 외 국가는 OECD·IMF·BIS. 첫 출처에 데이터가 없으면 다음 출처로 자동 전환
    2. ECOS 통계표: stat_code(+cycle, item_code1~4)
    3. SDMX 데이터플로: source(OECD|IMF|BIS) + dataflow + key + cycle

    ★ validation: 국가·주기·단위(지수 기준)·배수·기간·결측·중복·개정을 검사합니다(pass/info/warn/fail)
    ★ cross_validate=True: 같은 개념을 가진 모든 출처를 조회해 시점별 값·차이를 대조합니다(표준 개념만)
    ★ 날짜: "2024", "2024-03", "2024Q1", "2024-03-15" 등 자동 변환. 생략 시 일별 3개월, 그 외 2년
    ★ transform: "yoy"(전년동기비 %) / "pop"(전기비 %) — value가 증감률이 되고 원값은 level 열
    ★ rebase_period: 지수를 해당 시점=100으로 재기준화 (예: "2020")

    Args:
        indicator: 표준 개념 id/이름 (search_statistics(scope="concepts")로 확인)
        country: 국가 ISO 코드 (표준 개념 조회 시 기본값 KR; SDMX 직접 조회 시 지정하면 국가 검증에 사용)
        source: ECOS | OECD | IMF | BIS
        stat_code: ECOS 통계표코드
        cycle: 주기 A/S/Q/M/SM/D
        item_code1~4: ECOS 항목코드
        dataflow: SDMX 데이터플로 (예: "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)")
        key: SDMX 시계열 키 (예: "USA.M.IRLT.PA._Z._Z._Z._Z.N")
        start_date: 시작 시점
        end_date: 종료 시점
        recent_years: 날짜 생략 시 기간
        transform: "yoy" | "pop" | "none"
        changes_only: 값이 바뀐 시점만 표시 (기준금리 개념은 기본 적용)
        rebase_period: 지수 재기준 시점
        unit_mult: 값의 배수를 10^unit_mult 단위로 환산 (예: 십억원(9) 시계열에 12 → 조 단위)
        cross_validate: 기관 간 교차검증 결과 포함
        output_format: "compact" | "csv" | "json" | "sdmx"
        prefer_latest: ECOS 결과가 잘릴 때 최신 구간 우선
        start_count: ECOS 조회 시작 순번
        end_count: ECOS 조회 끝 순번
        language: "kr" | "en"

    Returns:
        series(시점·값), provenance(출처·인용문), validation(검증 결과), 선택 시 cross_validation
    """
    fmt = _choice(output_format, FORMATS, "output_format", "compact")
    service = _service(ctx)
    spec = SeriesSpec(
        indicator=indicator, country=country, source=source, stat_code=stat_code, cycle=cycle,
        item_code1=item_code1, item_code2=item_code2, item_code3=item_code3, item_code4=item_code4,
        dataflow=dataflow, key=key, transform=transform,
    )

    async def run() -> str:
        candidates = await _resolve(service, spec, changes_only=changes_only)
        loaded = await service.load_first(
            candidates,
            lambda src: _date_window(src.freq, start_date, end_date, recent_years),
            language=language,
            start_count=start_count,
            end_count=end_count,
            prefer_latest=prefer_latest,
        )
        _harmonize(loaded, rebase_period, unit_mult)
        src = loaded.source
        header: dict[str, Any] = {
            "concept": {"id": src.concept.id, "name": src.concept.name_ko} if src.concept else None,
            "country": src.country.iso2 if src.country else None,
            "source": src.describe(),
            "start_date": loaded.start,
            "end_date": loaded.end,
        }
        if loaded.attempts:
            header["fallback_attempts"] = loaded.attempts
        extra = None
        if cross_validate:
            if not (src.concept and src.country):
                raise ResolutionError("cross_validate는 표준 개념(indicator)으로 조회할 때만 쓸 수 있습니다.")
            result = await service.cross_check(
                src.concept, src.country, lambda s: _date_window(s.freq, start_date, end_date, recent_years)
            )
            extra = {"cross_validation": result}
        return render(
            loaded.series,
            loaded.reports,
            fmt,
            header=header,
            declared_unit=src.expected_unit,
            changes_only=src.changes_only,
            language=language,
            extra=extra,
        )

    return await _guard(run())


# ── Tool 4: compare ─────────────────────────────────────────────────

@mcp.tool(title="시계열 비교·교차검증", annotations=READ_ONLY, structured_output=False)
async def compare_series(
    ctx: Context,
    series: list[SeriesSpec],
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int = 3,
    frequency: str | None = None,
    aggregation: str = "mean",
    normalize_method: str = "none",
    join: str = "inner",
    output_format: str = "compact",
) -> str:
    """여러 시계열(2~6개)을 같은 주기로 맞춰 비교합니다. 국가 간·기관 간·지표 간 비교 모두 가능합니다.

    예:
    - 국가 비교: [{"indicator":"POLICY_RATE","country":"KR"}, {"indicator":"POLICY_RATE","country":"US"}]
    - 기관 대조: [{"indicator":"CPI","source":"ECOS"}, {"indicator":"CPI","source":"IMF"}]
      → 같은 개념·국가를 다른 기관에서 가져오면 cross_validation(시점별 값·차이)이 자동 포함됩니다
    - 지표 관계: [{"indicator":"POLICY_RATE"}, {"indicator":"CPI_YOY"}]

    Args:
        series: 계열 목록 (각 항목: indicator/country/source 또는 stat_code/... 또는 source/dataflow/key/cycle)
        start_date: 시작 시점 (생략 시 최근 recent_years년)
        end_date: 종료 시점
        recent_years: 기간 (기본값 3)
        frequency: 비교 주기 (생략 시 가장 낮은 빈도)
        aggregation: "mean" | "last" | "first" | "sum"
        normalize_method: "none" | "index"(첫 시점=100) | "zscore"
        join: "inner" | "outer"
        output_format: "compact" | "csv"

    Returns:
        정렬된 비교표, 상관계수, 계열별 출처·검증, 해당 시 교차검증 결과
    """
    if not 2 <= len(series) <= 6:
        raise ToolError("series는 2~6개를 지정하세요.")
    how = _choice(aggregation, AGGREGATIONS, "aggregation", "mean")
    norm = _choice(normalize_method, NORMALIZATIONS, "normalize_method", "none")
    join_how = _choice(join, {"inner", "outer"}, "join", "inner")
    fmt = _choice(output_format, {"compact", "csv"}, "output_format", "compact")
    service = _service(ctx)

    async def run() -> str:
        candidate_lists = [await _resolve(service, spec, changes_only=False) for spec in series]
        target = frequency.strip().upper() if frequency else coarsest_cycle([c[0].freq for c in candidate_lists])
        # Prefer sources already at the comparison frequency (e.g. monthly policy rate over daily).
        candidate_lists = [sorted(c, key=lambda src: src.freq != target) for c in candidate_lists]
        if target not in VALID_CYCLES:
            raise ResolutionError(f"유효하지 않은 frequency입니다: '{frequency}'.")
        for c in candidate_lists:
            if not can_convert(c[0].freq, target):
                raise ResolutionError(
                    f"주기 {c[0].freq}를 더 높은 빈도 {target}로 변환할 수 없습니다. frequency를 생략하거나 더 낮은 빈도를 지정하세요."
                )
        window_start, window_end = _date_window(target, start_date, end_date, recent_years)
        ecos_start, ecos_end = to_ecos_period(window_start, target), to_ecos_period(window_end, target)

        def window(src: ResolvedSource) -> tuple[str, str]:
            return (
                ecos_to_canonical(to_cycle(ecos_start, src.freq, "start"), src.freq),
                ecos_to_canonical(to_cycle(ecos_end, src.freq, "end"), src.freq),
            )

        loaded_list = await asyncio.gather(*(service.load_first(c, window) for c in candidate_lists))

        labels: list[str] = []
        converted: list[list[tuple[str, float]]] = []
        info: list[dict[str, Any]] = []
        for spec, loaded in zip(series, loaded_list):
            if len(loaded.series) > 1:
                names = ", ".join(s.title for s in loaded.series[:4])
                raise ResolutionError(f"'{spec.label or spec.indicator or spec.stat_code or spec.dataflow}'가 여러 계열을 반환합니다({names}). 코드를 더 지정하세요.")
            s = loaded.series[0]
            points = [(to_ecos_period(p, s.freq), v) for p, v in s.points()]
            points = convert_frequency(points, s.freq, target, how)
            points = normalize([(ecos_to_canonical(p, target), v) for p, v in points], norm)
            label = spec.label or (
                f"{loaded.source.concept.id}:{s.ref_area}:{s.provider}" if loaded.source.concept else s.title
            )
            while label in labels:
                label += "'"
            labels.append(label)
            converted.append(points)
            info.append(
                {
                    "label": label,
                    "series_id": s.series_id,
                    "country": s.ref_area,
                    "unit": s.unit or loaded.source.expected_unit,
                    "source_freq": s.freq,
                    "validation": loaded.reports[0].compact()["status"],
                    "citation": s.provenance.citation(s.title),
                }
            )

        table = align(converted, join_how)
        out: dict[str, Any] = {
            "frequency": target,
            "aggregation": how,
            "start_date": window_start,
            "end_date": window_end,
            **({"normalize": norm} if norm != "none" else {}),
            "series": info,
            "correlation": correlation_matrix(labels, table),
        }
        # Same concept and country from different providers → cross-validation.
        groups: dict[tuple[str, str], list[LoadedSeries]] = {}
        for loaded in loaded_list:
            src = loaded.source
            if src.concept and src.country:
                groups.setdefault((src.concept.id, src.country.iso2), []).append(loaded)
        checks = []
        for (concept_id, country_code), group in groups.items():
            if len({g.source.provider for g in group}) > 1:
                checks.append(
                    cross_validate(
                        [g.series[0] for g in group],
                        concept_id=concept_id,
                        country=country_code,
                        unit=group[0].source.concept.unit,
                        ledger=service.ledger,
                    )
                )
        if checks:
            out["cross_validation"] = checks

        if fmt == "csv":
            buffer = io.StringIO()
            buffer.write(f"# frequency: {target}, aggregation: {how}\n")
            for pair, stats in out["correlation"].items():
                buffer.write(f"# corr {pair}: r={stats['r']} (n={stats['n']})\n")
            for i in info:
                buffer.write(f"# source {i['label']}: {i['citation']} | validation {i['validation']}\n")
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(["PERIOD", *labels])
            writer.writerows(["" if v is None else v for v in row] for row in table)
            return buffer.getvalue().rstrip("\n")
        return dumps({**out, "columns": ["period", *labels], "rows": table})

    return await _guard(run())


# ── Tool 5: statistics ──────────────────────────────────────────────

@mcp.tool(title="기술통계·추세 계산", annotations=READ_ONLY, structured_output=False)
async def calculate_statistics(
    ctx: Context,
    indicator: str | None = None,
    country: str | None = None,
    source: str | None = None,
    stat_code: str | None = None,
    cycle: str | None = None,
    item_code1: str | None = None,
    item_code2: str | None = None,
    item_code3: str | None = None,
    item_code4: str | None = None,
    dataflow: str | None = None,
    key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int | None = None,
) -> str:
    """시계열의 요약통계를 계산합니다 (get_data와 같은 방식으로 대상을 지정).

    계열별: count, first/last, min/max(시점), mean, median, std, change/change_pct, cagr_pct,
    trend_per_year/trend_r2, latest_pop_pct·pop_pct_std, max_drawdown_pct, latest_yoy_pct·mean_yoy_pct.
    검증 결과와 출처가 함께 반환됩니다.

    Args:
        indicator: 표준 개념 id/이름
        country: 국가 ISO 코드 (표준 개념 조회 시 기본값 KR)
        source: ECOS | OECD | IMF | BIS
        stat_code: ECOS 통계표코드
        cycle: 주기
        item_code1~4: ECOS 항목코드
        dataflow: SDMX 데이터플로
        key: SDMX 시계열 키
        start_date: 시작 시점
        end_date: 종료 시점
        recent_years: 기간

    Returns:
        계열별 요약통계, 검증 상태, 출처
    """
    service = _service(ctx)
    spec = SeriesSpec(
        indicator=indicator, country=country, source=source, stat_code=stat_code, cycle=cycle,
        item_code1=item_code1, item_code2=item_code2, item_code3=item_code3, item_code4=item_code4,
        dataflow=dataflow, key=key,
    )

    async def run() -> str:
        candidates = await _resolve(service, spec, changes_only=False)
        loaded = await service.load_first(candidates, lambda src: _date_window(src.freq, start_date, end_date, recent_years))
        results = []
        for s, report in zip(loaded.series, loaded.reports):
            points = s.points()
            yoy = None
            is_rate = (s.unit or loaded.source.expected_unit or "").startswith("PC")
            if s.freq in PERIODS_PER_YEAR and not is_rate:
                lag = PERIODS_PER_YEAR[s.freq]
                by_index = {period_to_index(s.freq, to_ecos_period(p, s.freq)): (p, v) for p, v in points}
                yoy = [
                    (p, round((v / by_index[i - lag][1] - 1) * 100, 2))
                    for i, (p, v) in sorted(by_index.items())
                    if i - lag in by_index and by_index[i - lag][1]
                ]
            ecos_points = [(to_ecos_period(p, s.freq), v) for p, v in points]
            stats = describe(s.freq, ecos_points, yoy, is_rate=is_rate)
            for k in ("first", "last", "min", "max"):
                if k in stats:
                    stats[k]["time"] = ecos_to_canonical(stats[k]["time"], s.freq)
            entry = {
                "series_id": s.series_id,
                "title": s.title,
                "country": s.ref_area,
                "unit": s.unit or loaded.source.expected_unit,
                "freq": s.freq,
                "stats": stats,
                "validation": report.compact(),
                "citation": s.provenance.citation(s.title),
            }
            if is_rate:
                entry["note_units"] = "비율 지표라 변화는 %p(퍼센트포인트)로 계산했습니다"
            if s.truncated:
                entry["truncated"] = True
            if s.notes:
                entry["notes"] = s.notes
            results.append(entry)
        src = loaded.source
        return dumps(
            {
                "concept": {"id": src.concept.id, "name": src.concept.name_ko} if src.concept else None,
                "source": src.describe(),
                "start_date": loaded.start,
                "end_date": loaded.end,
                "series": results,
            }
        )

    return await _guard(run())


# ── Tool 6: explain ─────────────────────────────────────────────────

def _meta_candidates(term: str, table_name: str | None) -> list[str]:
    names = [term.strip()]
    if table_name:
        stripped = table_name.split(". ", 1)[-1] if ". " in table_name else table_name
        names += [stripped, stripped.split("(")[0].strip()]
    return list(dict.fromkeys(n for n in names if n))[:3]


@mcp.tool(title="지표 설명", annotations=READ_ONLY, structured_output=False)
async def explain_indicator(
    ctx: Context,
    term: str,
    country: str = "KR",
    stat_code: str | None = None,
    language: str = "kr",
) -> str:
    """지표의 뜻과 측정 방식, 국가별로 쓸 수 있는 출처(기관·데이터플로·키)를 설명합니다.

    표준 개념 정의(한/영), 개념의 출처 매핑과 국가별 키, 한국은행 통계용어사전 정의와
    통계 설명자료(작성기관·작성방법), 관련 ECOS 통계표를 한 번에 반환합니다.

    Args:
        term: 지표·용어 (예: "CPI_YOY", "기준금리", "경제심리지수", "unemployment")
        country: 출처 키를 보여줄 국가 (기본값 KR)
        stat_code: 특정 ECOS 통계표를 설명할 때
        language: "kr" | "en"

    Returns:
        concept(정의·단위·출처), definition(용어사전), methodology(설명자료), tables
    """
    service = _service(ctx)
    client = service.ecos_client
    term = term.strip()
    if not term:
        raise ToolError("term을 입력하세요.")
    out: dict[str, Any] = {"term": term}

    concept = find_concept(term)
    try:
        ctry = service.resolve_country(country) if country else None
    except ResolutionError as e:
        raise ToolError(str(e)) from e
    if concept:
        out["concept"] = concept.to_dict()
        if ctry:
            out["sources_for_country"] = [
                {
                    "provider": m.provider,
                    "dataflow": m.dataflow,
                    "dataflow_name": dataflow_name(m.provider, m.dataflow) if m.provider != "ECOS" else None,
                    "key": m.render_key(ctry),
                    "freq": m.freq,
                    "unit": m.unit,
                    **({"transform": m.transform} if m.transform else {}),
                }
                for m in concept.sources_for(ctry)
            ]
    tables = client.search_statistic_tables(term, searchable_only=True, limit=5)["rows"] if not concept or (ctry and ctry.iso2 == "KR") else []
    ecos_code = (stat_code or "").strip() or (
        next((m.dataflow for m in concept.sources if m.provider == "ECOS"), None) if concept else None
    ) or (tables[0]["STAT_CODE"] if tables else None)
    info = client.table_info(ecos_code) if ecos_code else None

    async def glossary() -> list[dict[str, Any]]:
        words = [term] + ([concept.name_ko.split("(")[0]] if concept else [])
        for word in dict.fromkeys(words):
            try:
                res = await client.search_statistic_word(word, language=language, end_count=3)
            except EcosApiError:
                continue
            if res["rows"]:
                return [{"word": r.get("WORD"), "definition": r.get("CONTENT")} for r in res["rows"]]
        return []

    async def methodology() -> list[dict[str, Any]]:
        for name in _meta_candidates(concept.name_ko.split("(")[0] if concept else term, (info or {}).get("STAT_NAME")):
            try:
                res = await client.get_all_statistic_meta(name, language=language)
            except EcosApiError:
                continue
            entries = [
                {"section": r.get("CONT_NAME"), "text": str(r["META_DATA"])[:600]} for r in res["rows"] if r.get("META_DATA")
            ]
            if entries:
                return [{"dataset": name}, *entries[:10]]
        return []

    definition, methods = await asyncio.gather(glossary(), methodology())
    if definition:
        out["definition"] = definition
    if methods:
        out["methodology"] = methods
    if tables:
        out["tables"] = _compact_rows([{k: t.get(k) for k in ("STAT_CODE", "STAT_NAME", "CYCLE")} for t in tables])
    if len(out) == 1:
        raise ToolError(f"'{term}'에 대한 정보를 찾지 못했습니다. search_statistics로 다른 검색어를 시도하세요.")
    return dumps(out)


# ── Resources ───────────────────────────────────────────────────────

@mcp.resource("gesm://concepts")
def concepts_resource() -> str:
    """Concept Catalog: 표준 개념과 기관별 출처 매핑(키 템플릿·단위·기준)."""
    return dumps({"concepts": [c.to_dict() for c in CONCEPTS]})


@mcp.resource("gesm://countries")
def countries_resource() -> str:
    """키 템플릿에 쓰이는 국가 코드(ISO2/ISO3/통화)."""
    return dumps({"countries": [c.__dict__ for c in all_countries()]})


@mcp.resource("gesm://providers")
def providers_resource() -> str:
    """연동 기관(ECOS, OECD, IMF, BIS)의 엔드포인트와 형식."""
    return dumps(
        {
            "ECOS": {"endpoint": "https://ecos.bok.or.kr/api", "format": "ECOS REST JSON (SDMX 미제공 → 서버에서 SDMX 개념으로 매핑)", "key": "ECOS_API_KEY"},
            **{
                k: {"endpoint": v.data_base, "structure": v.structure_base, "sdmx_api": v.api, "format": v.data_accept, "attribution": v.attribution}
                for k, v in SOURCES.items()
            },
        }
    )


@mcp.resource("gesm://validation/summary")
def validation_summary_resource() -> str:
    """검증 원장(ledger)에 쌓인 기관 간 교차검증 결과의 일치율 요약."""
    return dumps(ValidationLedger().summary())


@mcp.resource("gesm://sdmx/ecos-conventions", mime_type="text/plain")
def sdmx_conventions_resource() -> str:
    """ECOS → SDMX 매핑 규칙."""
    return ecos_sdmx.__doc__ or ""


@mcp.resource("gesm://date-format-guide")
def date_format_guide_resource() -> str:
    """주기별 날짜 형식."""
    return dumps({"cycles": CYCLE_DESCRIPTIONS, "note": "'2024', '2024-03', '2024Q1', '2024-03-15' 등은 자동 변환됩니다."})


# ── Prompts ─────────────────────────────────────────────────────────

@mcp.prompt(name="macro-economic-briefing")
def macro_economic_briefing(country: str = "KR") -> str:
    """국가 거시경제 브리핑."""
    return (
        f"{country}의 거시경제 현황을 근거 기반으로 브리핑해주세요.\n"
        f"1. calculate_statistics로 GDP_REAL_GROWTH_QOQ, CPI_YOY, POLICY_RATE, UNEMPLOYMENT_RATE_SA를 country='{country}'로 요약합니다.\n"
        "2. 각 응답의 validation 상태를 확인하고, warn/fail이 있으면 보고서에 명시합니다.\n"
        f"3. get_data(indicator='CPI_YOY', country='{country}', cross_validate=True)로 기관 간 일치 여부를 확인합니다.\n"
        "4. 모든 수치 옆에 provenance의 citation(기관·데이터셋·조회시각)을 붙여 보고서를 작성하세요."
    )


@mcp.prompt(name="compare-countries")
def compare_countries(indicator: str = "POLICY_RATE", countries: str = "KR,US,JP") -> str:
    """여러 국가의 같은 지표 비교."""
    items = ", ".join(f'{{"indicator":"{indicator}","country":"{c.strip()}"}}' for c in countries.split(","))
    return (
        f"{countries} 국가들의 {indicator}를 비교 분석해주세요.\n"
        f"1. explain_indicator(term='{indicator}')로 개념과 국가별 출처를 확인합니다.\n"
        f"2. compare_series(series=[{items}])로 같은 주기로 맞춰 비교합니다.\n"
        "3. 기관·기준시점·계절조정 차이와 validation 결과를 짚고, 출처를 인용해 결론을 제시하세요."
    )


@mcp.prompt(name="analyze-economic-trend")
def analyze_economic_trend(indicator_name: str = "CPI", country: str = "KR") -> str:
    """특정 지표의 추이 분석."""
    return (
        f"{country}의 '{indicator_name}' 추이를 분석해주세요.\n"
        f"1. explain_indicator(term='{indicator_name}', country='{country}')로 정의와 출처를 확인합니다.\n"
        f"2. get_data(indicator='{indicator_name}', country='{country}', recent_years=3)로 조회하고 validation을 확인합니다.\n"
        f"3. calculate_statistics로 변화율·추세·변동성을 계산합니다.\n"
        "4. 변곡점과 배경, 시사점을 설명하고 수치마다 citation을 붙이세요."
    )


# ── CLI ─────────────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 12 else "****"


async def run_health_check() -> int:
    print("=" * 65)
    print(f"🩺 Global Economic Statistical MCP {__version__} — 자가 진단")
    print("=" * 65)
    print(f"\n[1/3] Python {sys.version.split()[0]} ({sys.platform})")
    if ECOS_API_KEY == SAMPLE_API_KEY:
        print("[2/3] ECOS 키: ⚠️ sample 키 (1회 10건 제한) — 정식 키: https://ecos.bok.or.kr/api/#/")
    else:
        print(f"[2/3] ECOS 키: ✅ 설정됨 ({_mask_key(ECOS_API_KEY)})")
    print("\n[3/3] 공급자 연결 확인 (각 1건 조회)...")
    service = StatService()
    failures = 0
    probes = [("ECOS", "POLICY_RATE", "KR"), ("BIS", "POLICY_RATE", "US"), ("IMF", "CPI", "US"), ("OECD", "LONG_TERM_RATE", "US")]
    try:
        for provider, concept, country in probes:
            try:
                candidates = service.resolve(indicator=concept, country=country, source=provider, freq="M")
                loaded = await service.load(candidates[0], *_date_window("M", None, None, 1), record=False)
                last = loaded.series[0].observations[-1] if loaded.series and loaded.series[0].observations else None
                print(f"  ✅ {provider:4} {concept}({country}) 최신 {last.period if last else '-'} = {last.value if last else '-'}")
            except Exception as e:  # noqa: BLE001 - diagnostics report every failure
                failures += 1
                print(f"  ❌ {provider:4} {concept}({country}): {e}")
    finally:
        await service.close()
    print("\n" + "=" * 65)
    print("🎉 모든 진단을 통과했습니다." if not failures else f"⚠️ {failures}개 공급자 연결에 실패했습니다.")
    print("=" * 65)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="global-economic-statistical-mcp",
        description="ECOS + OECD·IMF·BIS(SDMX) 거시경제 통계 MCP 서버. 옵션 없이 실행하면 stdio MCP 서버로 동작합니다.",
        epilog="ECOS API 키는 ECOS_API_KEY 환경변수로 설정합니다 (미설정 시 sample 키). OECD·IMF·BIS는 키가 필요 없습니다.",
    )
    parser.add_argument("--check", "-c", action="store_true", help="API 키와 공급자 연결을 진단하고 종료합니다")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.check:
        sys.exit(asyncio.run(run_health_check()))
    mcp.run()


if __name__ == "__main__":
    main()
