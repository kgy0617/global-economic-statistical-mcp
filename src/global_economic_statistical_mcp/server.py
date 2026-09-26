"""Global Economic Statistical MCP server.

An AI-native interface for discovering, retrieving, comparing and analysing trusted
macroeconomic statistics across central banks and international organisations.

    LLM ── 6 MCP tools ── Concept Resolver ── Concept Catalog / Provider Catalog
                                   │
                           Provider Resolver
                 ┌─────────────────┴──────────────────┐
   ECOS (Bank of Korea REST)   SDMX: OECD · IMF · BIS · ECB · Eurostat   World Bank (Data360)
                 └─────────────────┬──────────────────┘
                            Canonical Model
                         (Validation + Provenance)
                                   │
                                Analysis

Tools:
- search_statistics: concepts, ECOS items/tables, SDMX dataflows, World Bank indicators, ECOS key statistics
- get_metadata: structure of an SDMX dataflow (DSD), a World Bank indicator, or an ECOS table (SDMX-mapped)
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
from global_economic_statistical_mcp.catalog.countries import (
    DEFAULT_COUNTRIES,
    Country,
    all_countries,
    get_country,
)
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

DATAFLOW_SOURCES = (*SOURCES, "WB")
from global_economic_statistical_mcp.service import (
    LoadedSeries,
    ResolutionError,
    ResolvedSource,
    StatService,
    canonical_provider,
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
        "Global economic statistics infrastructure for AI-powered macro research: discover, retrieve, compare "
        "and analyse trusted macroeconomic statistics from central banks and international organisations "
        "(Bank of Korea ECOS, OECD, IMF, BIS, ECB, Eurostat, World Bank) through one Concept Catalog and canonical "
        "time-series model. "
        "Every response carries provenance and validation results."
    ),
    instructions=(
        "Statistics infrastructure for global macroeconomic research. Verified economies: Korea (KR), United States (US), "
        "Japan (JP), China (CN), euro area (EA) and United Kingdom (GB). "
        "1) Ask for a concept and an economy: get_data(indicator='CPI_YOY', country='US'). country is required. "
        "Korea is served by the Bank of Korea (ECOS) first; other economies by OECD, IMF, BIS, ECB and Eurostat, "
        "and annual development indicators by the World Bank. Not every institution publishes every concept. "
        "2) For statistics without a concept, find an ECOS table or item, an SDMX dataflow or a World Bank indicator "
        "with search_statistics, "
        "check its key with get_metadata, then call get_data(stat_code=...) or get_data(source=..., dataflow=..., key=...). "
        "3) Read each response's validation (country, frequency, unit, scale, period, missing, duplicate, revision checks). "
        "For concepts with several sources use cross_validate=True: MATCH means the institutions agree, DIFFER means they "
        "differ for a documented reason, UNRESOLVED means they differ and nobody has explained why — report it, never pick silently. "
        "4) Cite provenance.citation next to every number."
    ),
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def _service(ctx: Context) -> StatService:
    return ctx.request_context.lifespan_context["service"]


def _choice(value: str | None, allowed: set[str], name: str, default: str) -> str:
    choice = (value or default).strip().lower()
    if choice not in allowed:
        raise ToolError(f"Unsupported {name} '{value}'. Use one of: {', '.join(sorted(allowed))}.")
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
            f"{e}. Frequency '{freq}' uses {CYCLE_DATE_FORMATS[freq][1]}; '2024', '2024-03', '2024Q1' and "
            "'2024-03-15' are converted automatically."
        ) from e
    if period_to_index(freq, start) > period_to_index(freq, end):
        raise ResolutionError(f"start_date ('{start}') is after end_date ('{end}').")
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
    """One series: a concept for a country, an ECOS table, an SDMX dataflow or a World Bank indicator."""

    indicator: str | None = Field(None, description="Concept id or name (e.g. 'CPI_YOY', 'policy rate')")
    country: str | None = Field(None, description="Economy: ISO code or EA for the euro area (required with indicator)")
    source: str | None = Field(None, description="ECOS | OECD | IMF | BIS | ECB | EUROSTAT | WB (default: catalog priority)")
    stat_code: str | None = Field(None, description="ECOS table code (instead of indicator; Korea)")
    cycle: str | None = Field(None, description="Frequency A/S/Q/M/SM/D")
    item_code1: str | None = None
    item_code2: str | None = None
    item_code3: str | None = None
    item_code4: str | None = None
    dataflow: str | None = Field(None, description="SDMX dataflow (e.g. 'BIS:WS_CBPOL(1.0)')")
    key: str | None = Field(None, description="SDMX series key (e.g. 'M.US')")
    transform: str | None = Field(None, description="'yoy' | 'pop' | 'none'")
    label: str | None = Field(None, description="Name shown in the results")


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

@mcp.tool(title="Search statistics", annotations=READ_ONLY, structured_output=False)
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
    """Search concepts, SDMX dataflows, World Bank indicators and Bank of Korea (ECOS) tables and items. The first step before retrieval.

    scope:
    - "all" (default): concepts + ECOS items + ECOS tables + international dataflows
    - "concepts": country-agnostic concepts mapped across institutions (retrieve with get_data(indicator=..., country=...))
    - "items": ECOS items (e.g. '쌀' rice, '휘발유' gasoline) from an index generated from the ECOS API
    - "tables": ECOS tables (without query: children of the parent_code category)
    - "dataflows": OECD, IMF, BIS, ECB and Eurostat dataflows and World Bank databases (every word must match the
      English name or id; source narrows the institution), plus a live World Bank Data360 indicator search
    - "key_statistics": latest values of the Bank of Korea's 100 key statistics

    Args:
        query: search terms (e.g. "policy rate", "unemployment", "inflation", "물가")
        scope: "all" | "concepts" | "items" | "tables" | "dataflows" | "key_statistics"
        source: institution for dataflow search (OECD | IMF | BIS | ECB | EUROSTAT | WB)
        parent_code: ECOS table category code
        searchable_only: ECOS tables that can be queried only
        limit: maximum results per category
        language: language of key_statistics ("kr" | "en")

    Returns:
        results per category (concepts, items, tables, dataflows, key_statistics)
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
                raise ToolError(f"No ECOS table category '{parent_code}'.")
        out["tables"] = {
            "total_matches": res["total_matches"],
            "data": _compact_rows(
                [{k: t.get(k) for k in ("STAT_CODE", "STAT_NAME", "CYCLE", "SRCH_YN", "P_STAT_CODE")} for t in res["rows"]]
            ),
        }
    if scope in ("all", "dataflows") and text:
        provider = canonical_provider(source)
        if provider and provider not in DATAFLOW_SOURCES:
            raise ToolError(f"For dataflow search, source must be one of {', '.join(DATAFLOW_SOURCES)}.")
        out["dataflows"] = search_dataflows(text, provider=provider, limit=limit)
        if provider in (None, "WB"):
            try:
                out["world_bank_indicators"] = await service.providers["WB"].search(text, limit=min(limit, 10))
            except ProviderError as e:
                out["world_bank_indicators"] = {"error": str(e)}
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

@mcp.tool(title="Get structure (metadata)", annotations=READ_ONLY, structured_output=False)
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
    """Return the structure (dimensions and codelists) of a dataset, to find the codes get_data needs.

    - OECD, IMF, BIS, ECB, EUROSTAT: source + dataflow → the dimensions and codelists of the institution's own DSD.
      A series key is the codes joined with '.' in dimension order (e.g. BIS WS_CBPOL → 'M.US').
    - WB (World Bank): source="WB" + dataflow=<indicator id> → name, definition, unit, periodicity;
      data keys are INDICATOR.REF_AREA (e.g. dataflow="WB_WDI", key="WB_WDI_SP_POP_TOTL.USA").
    - ECOS: stat_code → the table's structure mapped to SDMX (FREQ + ITEM_CODE1..4, coverage, units).
      output_format="sdmx" returns an SDMX-JSON structure message.

    Args:
        stat_code: ECOS table code (e.g. "901Y009")
        source: OECD | IMF | BIS | ECB | EUROSTAT | WB (with dataflow)
        dataflow: SDMX dataflow (e.g. "BIS:WS_CBPOL(1.0)", "IMF.STA:CPI")
        code_keyword: filter codes by name or value (e.g. "Japan", "JPN", "current account")
        codes_limit: maximum codes per dimension (default 30)
        output_format: "compact" | "sdmx" (ECOS only)
        language: "kr" | "en" (ECOS names)

    Returns:
        dimensions, codes per dimension, a key template
    """
    fmt = _choice(output_format, {"compact", "sdmx"}, "output_format", "compact")
    service = _service(ctx)
    if dataflow:
        provider = canonical_provider(source) or ""
        if provider not in DATAFLOW_SOURCES:
            raise ToolError(f"With dataflow, set source to one of {', '.join(DATAFLOW_SOURCES)}.")
        if fmt == "sdmx":
            raise ToolError("The original structure of an institution's dataflow is available at structure_url. Use output_format='compact'.")
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
        raise ToolError(f"Specify source + dataflow ({', '.join(DATAFLOW_SOURCES)}) or stat_code (ECOS).")
    client = service.ecos_client
    code = stat_code.strip()
    info = client.table_info(code)
    if info and info.get("SRCH_YN") == "N":
        raise ToolError(
            f"'{code}' ({info.get('STAT_NAME')}) is a category. List its tables with search_statistics(scope='tables', parent_code='{code}')."
        )
    items = await _guard(client.list_all_statistic_items(code, language=language))
    if not items["rows"]:
        raise ToolError(f"No items found for table '{code}'. Check the code with search_statistics.")
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
            raise ResolutionError("rebase_period applies to index (IX) series only.")
        target = ecos_to_canonical(to_cycle(rebase_period, series.freq, "start"), series.freq)
        points = [(o.period, o.value) for o in series.observations]
        if target not in dict(points):
            raise ResolutionError(f"No value at the rebase period {target} (choose a period inside the requested window).")
        for obs, (_, value) in zip(series.observations, rebase_index(points, base_period=target)):
            obs.value = value
        series.base_period = target.replace("-", "")
        series.provenance.transformations.append(f"rebase: {target}=100")


@mcp.tool(title="Get data (validated, with provenance)", annotations=READ_ONLY, structured_output=False)
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
    """Retrieve a time series in the canonical model, with validation and provenance.

    Three ways to ask (use one):
    1. Concept: indicator + country (+ source, cycle), e.g. indicator="CPI_YOY", country="US".
       Korea uses ECOS first; other economies OECD, IMF, BIS, ECB, Eurostat and the World Bank.
       If a source has no data, the next one is tried.
    2. ECOS table (Korea): stat_code (+ cycle, item_code1..4)
    3. Institution dataflow: source (OECD | IMF | BIS | ECB | EUROSTAT | WB) + dataflow + key + cycle

    * validation: country, frequency, unit (and index base), scale, period, missing, duplicate and revision checks (pass/info/warn/fail)
    * cross_validate=True: fetch the concept from every source and compare period by period;
      the result is MATCH, DIFFER (documented cause) or UNRESOLVED (unexplained difference)
    * dates: "2024", "2024-03", "2024Q1", "2024-03-15" are converted; default window 3 months for daily data, else 2 years
    * transform: "yoy" / "pop" % change — value becomes the change and the level is kept
    * rebase_period: rebase an index to that period = 100 (e.g. "2020")

    Args:
        indicator: concept id or name (see search_statistics(scope="concepts"))
        country: economy, ISO code or EA (required with indicator; with a direct SDMX query it enables the country check)
        source: ECOS | OECD | IMF | BIS | ECB | EUROSTAT | WB (aliases such as "World Bank" work)
        stat_code: ECOS table code
        cycle: frequency A/S/Q/M/SM/D
        item_code1..4: ECOS item codes
        dataflow: SDMX dataflow (e.g. "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)") or World Bank database ("WB_WDI")
        key: SDMX series key (e.g. "USA.M.IRLT.PA._Z._Z._Z._Z.N")
        start_date: start period
        end_date: end period
        recent_years: window when dates are omitted
        transform: "yoy" | "pop" | "none"
        changes_only: only periods where the value changed (default for policy rates)
        rebase_period: index rebase period
        unit_mult: rescale values to 10^unit_mult (e.g. 12 turns a billions (9) series into trillions)
        cross_validate: include cross-validation across institutions
        output_format: "compact" | "csv" | "json" | "sdmx"
        prefer_latest: when ECOS results are truncated, keep the latest periods
        start_count: ECOS first row
        end_count: ECOS last row
        language: "kr" | "en" (ECOS names)

    Returns:
        series (periods and values), provenance (source and citation), validation, and optionally cross_validation
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
            "concept": _concept_header(src),
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
                raise ResolutionError("cross_validate requires a concept query (indicator + country).")
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

@mcp.tool(title="Compare series and cross-validate", annotations=READ_ONLY, structured_output=False)
async def compare_series(
    ctx: Context,
    series: list[SeriesSpec],
    start_date: str | None = None,
    end_date: str | None = None,
    recent_years: int = 3,
    frequency: str | None = None,
    aggregation: str | None = None,
    normalize_method: str = "none",
    join: str = "inner",
    output_format: str = "compact",
) -> str:
    """Align 2-6 series to one frequency and compare them: across economies, institutions or concepts.

    Examples:
    - economies: [{"indicator":"POLICY_RATE","country":"US"}, {"indicator":"POLICY_RATE","country":"EA"}]
    - institutions: [{"indicator":"CPI","country":"CN","source":"IMF"}, {"indicator":"CPI","country":"CN","source":"BIS"}]
      → the same concept and economy from different institutions adds cross_validation automatically
    - concepts: [{"indicator":"POLICY_RATE","country":"GB"}, {"indicator":"CPI_YOY","country":"GB"}]

    Args:
        series: list of series (indicator/country/source, or stat_code/..., or source/dataflow/key/cycle)
        start_date: start period (default: the last recent_years years)
        end_date: end period
        recent_years: window (default 3)
        frequency: comparison frequency (default: the lowest one)
        aggregation: "mean" | "last" | "first" | "sum" (default per concept: flows such as the current account
            or GDP are summed, stocks such as reserves take the period end, everything else is averaged)
        normalize_method: "none" | "index" (first period = 100) | "zscore"
        join: "inner" | "outer"
        output_format: "compact" | "csv"

    Returns:
        the aligned table, correlations, provenance and validation per series, and cross-validation where it applies
    """
    if not 2 <= len(series) <= 6:
        raise ToolError("Specify 2 to 6 series.")
    how = _choice(aggregation, AGGREGATIONS, "aggregation", "mean") if aggregation else None
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
            raise ResolutionError(f"Invalid frequency '{frequency}'.")
        for c in candidate_lists:
            if not can_convert(c[0].freq, target):
                raise ResolutionError(
                    f"Cannot convert frequency {c[0].freq} to the higher frequency {target}. Omit frequency or choose a lower one."
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
                raise ResolutionError(f"'{spec.label or spec.indicator or spec.stat_code or spec.dataflow}' returns several series ({names}). Specify more codes.")
            s = loaded.series[0]
            points = [(to_ecos_period(p, s.freq), v) for p, v in s.points()]
            series_how = how or (loaded.source.concept.aggregation if loaded.source.concept else "mean")
            points = convert_frequency(points, s.freq, target, series_how, complete_only=True)
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
                    **({"aggregation": series_how} if s.freq != target else {}),
                    "validation": loaded.reports[0].compact()["status"],
                    "citation": s.provenance.citation(s.title),
                }
            )

        table = align(converted, join_how)
        out: dict[str, Any] = {
            "frequency": target,
            "aggregation": how or "concept_default",
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
                        aggregation=group[0].source.concept.aggregation,
                        rebase=group[0].source.concept.compare_rebased,
                        ledger=service.ledger,
                    )
                )
        if checks:
            out["cross_validation"] = checks

        if fmt == "csv":
            buffer = io.StringIO()
            buffer.write(f"# frequency: {target}, aggregation: {how or 'concept_default'}\n")
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

@mcp.tool(title="Calculate statistics", annotations=READ_ONLY, structured_output=False)
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
    """Summary statistics of a series (addressed the same way as get_data).

    Per series: count, first/last, min/max (with periods), mean, median, std, change/change_pct, cagr_pct,
    trend_per_year/trend_r2, latest_pop_pct, pop_pct_std, max_drawdown_pct, latest_yoy_pct, mean_yoy_pct.
    Rates (interest rates, inflation) change in percentage points. Validation and provenance are included.

    Args:
        indicator: concept id or name
        country: economy, ISO code or EA (required with indicator)
        source: ECOS | OECD | IMF | BIS | ECB | EUROSTAT | WB
        stat_code: ECOS table code
        cycle: frequency
        item_code1..4: ECOS item codes
        dataflow: SDMX dataflow
        key: SDMX series key
        start_date: start period
        end_date: end period
        recent_years: window

    Returns:
        statistics, validation status and provenance per series
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
                entry["note_units"] = "rate series: changes are in percentage points"
            if s.truncated:
                entry["truncated"] = True
            if s.notes:
                entry["notes"] = s.notes
            results.append(entry)
        src = loaded.source
        return dumps(
            {
                "concept": _concept_header(src),
                "source": src.describe(),
                "start_date": loaded.start,
                "end_date": loaded.end,
                "series": results,
            }
        )

    return await _guard(run())


# ── Tool 6: explain ─────────────────────────────────────────────────

def _concept_header(src: ResolvedSource) -> dict[str, Any] | None:
    c = src.concept
    return {"id": c.id, "name": c.name_en, "name_ko": c.name_ko} if c else None


def _meta_candidates(term: str, table_name: str | None) -> list[str]:
    names = [term.strip()]
    if table_name:
        stripped = table_name.split(". ", 1)[-1] if ". " in table_name else table_name
        names += [stripped, stripped.split("(")[0].strip()]
    return list(dict.fromkeys(n for n in names if n))[:3]


@mcp.tool(title="Explain indicator", annotations=READ_ONLY, structured_output=False)
async def explain_indicator(
    ctx: Context,
    term: str,
    country: str | None = None,
    stat_code: str | None = None,
    language: str = "kr",
) -> str:
    """Explain what an indicator measures and which sources (institution, dataflow, key) serve each economy.

    Returns the concept definition, its source mappings with rendered keys, and for Korean statistics the
    Bank of Korea glossary definition, methodology notes and related ECOS tables.

    Args:
        term: indicator or term (e.g. "CPI_YOY", "policy rate", "unemployment", "경제심리지수")
        country: economy whose sources to show (default: every verified economy)
        stat_code: a specific ECOS table to explain
        language: "kr" | "en" (Bank of Korea glossary and methodology)

    Returns:
        concept (definition, unit, sources), sources per economy, definition (glossary), methodology, tables
    """
    service = _service(ctx)
    client = service.ecos_client
    term = term.strip()
    if not term:
        raise ToolError("term is required.")
    out: dict[str, Any] = {"term": term}

    concept = find_concept(term)
    try:
        ctry = service.resolve_country(country) if country else None
    except ResolutionError as e:
        raise ToolError(str(e)) from e
    if concept:
        out["concept"] = concept.to_dict()

        def sources(c: Country) -> list[dict[str, Any]]:
            return [
                {
                    "provider": m.provider,
                    "dataflow": m.dataflow,
                    "dataflow_name": dataflow_name(m.provider, m.dataflow) if m.provider != "ECOS" else None,
                    "key": m.render_key(c),
                    "freq": m.freq,
                    "unit": m.unit,
                    **({"transform": m.transform} if m.transform else {}),
                }
                for m in concept.sources_for(c)
            ]

        if ctry:
            out["sources_for_country"] = sources(ctry)
        else:
            out["sources_by_country"] = {c: sources(get_country(c)) for c in DEFAULT_COUNTRIES}
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
        raise ToolError(f"Nothing found for '{term}'. Try other terms with search_statistics.")
    return dumps(out)


# ── Resources ───────────────────────────────────────────────────────

@mcp.resource("gesm://concepts")
def concepts_resource() -> str:
    """Concept Catalog: concepts and their source mappings (key templates, units, base periods)."""
    return dumps({"concepts": [c.to_dict() for c in CONCEPTS]})


@mcp.resource("gesm://countries")
def countries_resource() -> str:
    """Economies: ISO codes, currencies, provider area codes, and the default set re-verified against the live APIs."""
    countries = [
        {**c.__dict__, "provider_codes": dict(c.provider_codes), "verified": c.iso2 in DEFAULT_COUNTRIES} for c in all_countries()
    ]
    return dumps({"default_countries": list(DEFAULT_COUNTRIES), "countries": countries})


@mcp.resource("gesm://providers")
def providers_resource() -> str:
    """Institutions (ECOS, OECD, IMF, BIS, ECB, Eurostat, World Bank): endpoints and formats."""
    return dumps(
        {
            "ECOS": {"endpoint": "https://ecos.bok.or.kr/api", "format": "ECOS REST JSON (no SDMX endpoint; mapped to SDMX concepts by this server)", "key": "ECOS_API_KEY"},
            **{
                k: {"endpoint": v.data_base, "structure": v.structure_base, "sdmx_api": v.api, "format": v.data_accept, "attribution": v.attribution}
                for k, v in SOURCES.items()
            },
            "WB": {"endpoint": "https://data360api.worldbank.org/data360", "format": "World Bank Data360 API JSON (not SDMX)", "attribution": "World Bank Data360 (https://data360.worldbank.org/)"},
        }
    )


@mcp.resource("gesm://validation/summary")
def validation_summary_resource() -> str:
    """Validation ledger: agreement between institutions per concept, economy and provider pair (retention window) and the latest status."""
    return dumps(ValidationLedger().summary())


@mcp.resource("gesm://sdmx/ecos-conventions", mime_type="text/plain")
def sdmx_conventions_resource() -> str:
    """ECOS → SDMX mapping conventions."""
    return ecos_sdmx.__doc__ or ""


@mcp.resource("gesm://date-format-guide")
def date_format_guide_resource() -> str:
    """Date formats per frequency."""
    return dumps({"cycles": CYCLE_DESCRIPTIONS, "note": "'2024', '2024-03', '2024Q1' and '2024-03-15' are converted automatically."})


# ── Prompts ─────────────────────────────────────────────────────────

@mcp.prompt(name="macro-economic-briefing")
def macro_economic_briefing(country: str) -> str:
    """Evidence-based macroeconomic briefing for one economy."""
    return (
        f"Write an evidence-based macroeconomic briefing for {country}.\n"
        f"1. Summarise GDP_REAL_GROWTH_QOQ, CPI_YOY, POLICY_RATE, UNEMPLOYMENT_RATE_SA and CURRENT_ACCOUNT with "
        f"calculate_statistics(country='{country}'). Skip concepts with no source (e.g. China's unemployment rate) and say so.\n"
        "2. Check each response's validation status and state any warn or fail in the briefing.\n"
        f"3. Check agreement between institutions with get_data(indicator='CPI_YOY', country='{country}', cross_validate=True); "
        "report DIFFER and UNRESOLVED results rather than choosing one number.\n"
        "4. Put the provenance citation (institution, dataset, retrieval time) next to every number."
    )


@mcp.prompt(name="compare-countries")
def compare_countries(indicator: str = "POLICY_RATE", countries: str = ",".join(DEFAULT_COUNTRIES)) -> str:
    """Compare one indicator across economies."""
    items = ", ".join(f'{{"indicator":"{indicator}","country":"{c.strip()}"}}' for c in countries.split(","))
    return (
        f"Compare {indicator} across {countries}.\n"
        f"1. Check the concept and each economy's sources with explain_indicator(term='{indicator}').\n"
        f"2. Align them with compare_series(series=[{items}]).\n"
        "3. Point out differences in institution, base period and seasonal adjustment and the validation results, "
        "then conclude with citations."
    )


@mcp.prompt(name="analyze-economic-trend")
def analyze_economic_trend(indicator_name: str, country: str) -> str:
    """Trend analysis of one indicator for one economy."""
    return (
        f"Analyse the trend of '{indicator_name}' in {country}.\n"
        f"1. Check the definition and sources with explain_indicator(term='{indicator_name}', country='{country}').\n"
        f"2. Retrieve it with get_data(indicator='{indicator_name}', country='{country}', recent_years=3) and check validation.\n"
        "3. Compute changes, trend and volatility with calculate_statistics.\n"
        "4. Explain turning points, their background and implications, citing every number."
    )


# ── CLI ─────────────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    return f"{key[:4]}...{key[-4:]}" if len(key) > 12 else "****"


async def run_health_check() -> int:
    print("=" * 65)
    print(f"🩺 Global Economic Statistical MCP {__version__} — self-check")
    print("=" * 65)
    print(f"\n[1/3] Python {sys.version.split()[0]} ({sys.platform})")
    if ECOS_API_KEY == SAMPLE_API_KEY:
        print("[2/3] ECOS key: ⚠️ sample key (10 rows per call) — register at https://ecos.bok.or.kr/api/#/")
    else:
        print(f"[2/3] ECOS key: ✅ set ({_mask_key(ECOS_API_KEY)})")
    print("\n[3/3] Provider connectivity (one request each)...")
    service = StatService()
    failures = 0
    probes = [
        ("ECOS", "POLICY_RATE", "KR", "M"),
        ("BIS", "POLICY_RATE", "US", "M"),
        ("IMF", "CPI", "US", "M"),
        ("OECD", "LONG_TERM_RATE", "US", "M"),
        ("ECB", "CPI", "EA", "M"),
        ("EUROSTAT", "UNEMPLOYMENT_RATE", "EA", "M"),
        ("WB", "POPULATION", "US", "A"),
    ]
    try:
        for provider, concept, country, freq in probes:
            try:
                candidates = service.resolve(indicator=concept, country=country, source=provider, freq=freq)
                loaded = await service.load(candidates[0], *_date_window(freq, None, None, 2), record=False)
                last = loaded.series[0].observations[-1] if loaded.series and loaded.series[0].observations else None
                if last is None:
                    raise ResolutionError("no data returned")
                print(f"  ✅ {provider:8} {concept}({country}) latest {last.period} = {last.value}")
            except Exception as e:  # noqa: BLE001 - diagnostics report every failure
                failures += 1
                print(f"  ❌ {provider:8} {concept}({country}): {e}")
    finally:
        await service.close()
    print("\n" + "=" * 65)
    print("🎉 All checks passed." if not failures else f"⚠️ {failures} provider(s) failed.")
    print("=" * 65)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="global-economic-statistical-mcp",
        description="Global Economic Statistical MCP server (Bank of Korea ECOS, OECD, IMF, BIS, ECB, Eurostat, World Bank). "
        "Without options it runs as a stdio MCP server.",
        epilog="Set the ECOS key in ECOS_API_KEY (the sample key is used otherwise). No other institution needs a key.",
    )
    parser.add_argument("--check", "-c", action="store_true", help="check the API key and provider connectivity, then exit")
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.check:
        sys.exit(asyncio.run(run_health_check()))
    mcp.run()


if __name__ == "__main__":
    main()
