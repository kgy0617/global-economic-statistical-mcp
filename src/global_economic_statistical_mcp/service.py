"""Orchestration: Concept Resolver → Provider Resolver → Adapter → Canonical → Validation.

The MCP tools in :mod:`server` are thin wrappers around :class:`StatService`.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from global_economic_statistical_mcp.catalog.concepts import (
    Concept,
    SourceMapping,
    find_concept,
    rank_concepts,
)
from global_economic_statistical_mcp.catalog.countries import (
    DEFAULT_COUNTRIES,
    Country,
    get_country,
)
from global_economic_statistical_mcp.config import (
    PERIODS_PER_YEAR,
    VALID_CYCLES,
    index_to_period,
)
from global_economic_statistical_mcp.ecos_client import EcosClient
from global_economic_statistical_mcp.model import (
    CanonicalSeries,
    Observation,
    ecos_to_canonical,
    period_index,
)
from global_economic_statistical_mcp.providers.base import ProviderError, SeriesRequest
from global_economic_statistical_mcp.providers.data360 import Data360Provider
from global_economic_statistical_mcp.providers.ecos import EcosProvider
from global_economic_statistical_mcp.providers.sdmx_rest import (
    SOURCE_ALIASES,
    SOURCES,
    SdmxHttp,
    SdmxProvider,
)
from global_economic_statistical_mcp.storage import RevisionStore, ValidationLedger
from global_economic_statistical_mcp.validation import (
    NOT_COMPARED,
    SEVERITY,
    Expectation,
    ValidationReport,
    cross_validate,
    report_records,
    validate_series,
)

PROVIDERS = ("ECOS", *SOURCES, "WB")
# Other names people use for a provider ("World Bank", "ESTAT", ...).
PROVIDER_ALIASES = {**SOURCE_ALIASES, "WORLD BANK": "WB", "WORLDBANK": "WB", "DATA360": "WB", "BOK": "ECOS"}


def canonical_provider(source: str | None) -> str | None:
    if not source or not source.strip():
        return None
    name = source.strip().upper().replace("_", " ")
    return PROVIDER_ALIASES.get(name, name)
TRANSFORM_UNITS = {"yoy": "PC_YOY", "pop": "PC_POP"}


class ResolutionError(ValueError):
    """The request cannot be mapped to a source; the message tells the user what to do."""


@dataclass
class ResolvedSource:
    provider: str
    dataflow: str
    key: str
    freq: str
    concept: Concept | None = None
    country: Country | None = None
    mapping: SourceMapping | None = None
    transform: str | None = None
    changes_only: bool = False
    label: str | None = None

    @property
    def expected_unit(self) -> str | None:
        if self.transform:
            return TRANSFORM_UNITS[self.transform]
        if self.mapping:
            return self.mapping.unit
        return None

    def expectation(self, start: str, end: str) -> Expectation:
        m = self.mapping
        return Expectation(
            country=self.country.iso2 if self.country else None,
            freq=self.freq,
            unit=self.expected_unit,
            unit_mult=m.unit_mult if m else None,
            base_period=m.base_period if m and not self.transform else None,
            adjustment=m.adjustment if m else None,
            start=start,
            end=end,
            concept_id=self.concept.id if self.concept else None,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "dataflow": self.dataflow,
            "key": self.key,
            "freq": self.freq,
            **({"transform": self.transform} if self.transform else {}),
            **({"note": self.mapping.note} if self.mapping and self.mapping.note else {}),
        }


@dataclass
class LoadedSeries:
    source: ResolvedSource
    series: list[CanonicalSeries]
    reports: list[ValidationReport]
    start: str
    end: str
    attempts: list[dict[str, Any]] = field(default_factory=list)


# ── Canonical period helpers ────────────────────────────────────────


def shift(period: str, freq: str, n: int) -> str:
    return ecos_to_canonical(index_to_period(freq, period_index(period, freq) + n), freq)


def transform_lookback(freq: str, transform: str) -> int:
    if transform == "yoy":
        if freq == "D":
            raise ResolutionError("transform='yoy' is not available for daily (D) series. Use 'pop' or a monthly concept.")
        return PERIODS_PER_YEAR[freq]
    return 10 if freq == "D" else 1


def apply_transform(series: CanonicalSeries, transform: str, base: list[Observation]) -> None:
    """Replace values with % changes; the original level is kept in ``source_value``."""
    freq = series.freq
    history = {period_index(o.period, freq): o.value for o in base}
    history.update({period_index(o.period, freq): o.value for o in series.observations})
    ordered = sorted(history)
    for obs in series.observations:
        idx = period_index(obs.period, freq)
        if transform == "yoy":
            prev = history.get(idx - PERIODS_PER_YEAR[freq])
        else:
            earlier = [i for i in ordered if i < idx and history[i] is not None]
            prev = history[earlier[-1]] if earlier else None
        current = obs.value
        obs.source_value = current
        obs.value = (
            round((current / prev - 1) * 100, 4)
            if isinstance(current, int | float) and isinstance(prev, int | float) and prev
            else None
        )
    series.unit = TRANSFORM_UNITS[transform]
    series.unit_label = "percent change, year on year" if transform == "yoy" else "percent change, period on period"
    series.unit_mult = 0
    series.base_period = None
    label = "% change, year on year" if transform == "yoy" else "% change, period on period"
    series.title = f"{series.title} [{label}]"
    series.provenance.transformations.append(
        f"{transform}: {label} computed by this server from the published levels (original values in source_value)"
    )


def invert(series: CanonicalSeries, unit: str) -> None:
    """Replace values with their reciprocal (ECB publishes US dollars per euro; the concept is euros per dollar)."""
    for obs in series.observations:
        if isinstance(obs.value, int | float) and obs.value:
            obs.source_value = obs.value
            obs.value = round(1 / obs.value, 8)
    published = series.unit
    series.unit, series.unit_label = unit, None
    series.provenance.transformations.append(f"invert: 1/value (the source publishes {published or 'the reciprocal'}; original in source_value)")


def drop_unchanged(observations: list[Observation]) -> list[Observation]:
    """Keep the first/last observation and every value change (for step-like series)."""
    kept = []
    previous: object = object()
    for i, obs in enumerate(observations):
        if i in (0, len(observations) - 1) or obs.value != previous:
            kept.append(obs)
        previous = obs.value
    return kept


# ── Service ─────────────────────────────────────────────────────────


class StatService:
    def __init__(
        self,
        ecos_client: EcosClient | None = None,
        revisions: RevisionStore | None = None,
        ledger: ValidationLedger | None = None,
    ) -> None:
        self.ecos_client = ecos_client or EcosClient()
        self.http = SdmxHttp()
        self.providers: dict[str, Any] = {"ECOS": EcosProvider(self.ecos_client)}
        self.providers.update({k: SdmxProvider(v, self.http) for k, v in SOURCES.items()})
        self.providers["WB"] = Data360Provider(self.http)
        self.revisions = revisions or RevisionStore()
        self.ledger = ledger or ValidationLedger()

    async def close(self) -> None:
        await self.ecos_client.close()
        await self.http.close()

    # ── Resolution ──────────────────────────────────────────────────

    def resolve_concept(self, indicator: str) -> Concept:
        concept = find_concept(indicator)
        if concept:
            return concept
        ranked = rank_concepts(indicator)
        if ranked:
            listed = ", ".join(f"{c.id} ({c.name_en})" for _, c in ranked[:6])
            raise ResolutionError(f"'{indicator}' matches several concepts: {listed}. Use a concept_id or a more specific term.")
        raise ResolutionError(
            f"No concept matches '{indicator}'. Use search_statistics to find concepts, ECOS tables or SDMX dataflows."
        )

    def resolve_country(self, country: str | None) -> Country:
        if not country or not country.strip():
            raise ResolutionError(
                "country is required for concept queries (e.g. country='US'; 'EA' for the euro area). "
                f"Verified economies: {', '.join(DEFAULT_COUNTRIES)}. Only direct ECOS queries (stat_code) default to KR."
            )
        c = get_country(country)
        if not c:
            raise ResolutionError(f"Unknown country '{country}'. Use an ISO code (US, JP, KOR, ...) or EA for the euro area.")
        return c

    def resolve(
        self,
        *,
        indicator: str | None = None,
        country: str | None = None,
        source: str | None = None,
        freq: str | None = None,
        stat_code: str | None = None,
        item_codes: list[str | None] | None = None,
        dataflow: str | None = None,
        key: str | None = None,
        transform: str | None = None,
        changes_only: bool | None = None,
        label: str | None = None,
        default_ecos_cycle: str | None = None,
    ) -> list[ResolvedSource]:
        """Candidate sources in priority order (the first one that returns data is used)."""
        provider = canonical_provider(source)
        if provider and provider not in PROVIDERS:
            raise ResolutionError(f"Unsupported source '{source}'. Use one of {', '.join(PROVIDERS)}.")
        freq = freq.strip().upper() if freq else None
        if freq and freq not in VALID_CYCLES:
            raise ResolutionError(f"Invalid frequency '{freq}'. Use one of A, S, Q, M, SM, D.")
        transform = (transform or "").strip().lower() or None
        if transform in ("none", "raw"):
            transform = "none"
        if transform not in (None, "none", "yoy", "pop"):
            raise ResolutionError(f"Unsupported transform '{transform}'. Use 'yoy', 'pop' or 'none'.")

        if indicator and indicator.strip():
            if stat_code or dataflow:
                raise ResolutionError("indicator cannot be combined with stat_code/dataflow. Specify only one.")
            if item_codes and any(item_codes):
                raise ResolutionError("item_code cannot be used with indicator. For a direct query use stat_code.")
            concept = self.resolve_concept(indicator)
            ctry = self.resolve_country(country)
            if transform is not None and concept.unit in TRANSFORM_UNITS.values():
                raise ResolutionError(
                    f"{concept.id} is already a rate of change, so transform cannot be applied. "
                    "To compute one from levels, apply transform to a level concept such as CPI."
                )
            mappings = concept.sources_for(ctry, provider=provider, freq=freq)
            if transform == "yoy" and mappings:
                feasible = [m for m in mappings if m.freq != "D"]
                if not feasible:
                    raise ResolutionError(
                        f"Every selected source of {concept.id} is daily (D), so transform='yoy' is not available. Specify cycle='M'."
                    )
                mappings = feasible
            if not mappings:
                available = sorted({(s.provider, s.freq) for s in concept.sources_for(ctry)})
                everywhere = sorted({(s.provider, s.freq) for s in concept.sources})
                unpublished = sorted({s.provider for s in concept.sources if ctry.iso2 in s.excludes})
                euro_area = get_country("EA")
                hint = (
                    f". {ctry.iso2} is a euro-area member; the euro-area series may apply: country='EA'"
                    if ctry.currency == "EUR" and ctry.iso2 != "EA" and concept.sources_for(euro_area)
                    else ""
                )
                raise ResolutionError(
                    f"No source for {concept.id} ({concept.name_en}) in {ctry.iso2}"
                    + (f" from {provider}" if provider else "")
                    + (f" at frequency {freq}" if freq else "")
                    + f". Available (source, frequency) for {ctry.iso2}: {available or 'none'}; "
                    f"all: {everywhere}"
                    + (f". {', '.join(unpublished)} does not publish this statistic for {ctry.iso2}" if unpublished else "")
                    + hint
                )
            return [
                ResolvedSource(
                    provider=m.provider,
                    dataflow=m.dataflow,
                    key=m.render_key(ctry),
                    freq=m.freq,
                    concept=concept,
                    country=ctry,
                    mapping=m,
                    transform=m.transform if transform is None else (None if transform == "none" else transform),
                    changes_only=m.changes_only if changes_only is None else changes_only,
                    label=label or concept.name_en,
                )
                for m in mappings
            ]

        explicit_transform = None if transform in (None, "none") else transform
        if explicit_transform == "yoy" and freq == "D":
            raise ResolutionError("transform='yoy' is not available for daily (D) series. Use 'pop' or a monthly cycle.")
        if stat_code and stat_code.strip():
            if provider not in (None, "ECOS"):
                raise ResolutionError("stat_code is an ECOS table code. For international data use dataflow and key.")
            cycle = freq or default_ecos_cycle
            if not cycle:
                raise ResolutionError(f"Unknown frequency for table '{stat_code}'. Specify cycle.")
            if explicit_transform == "yoy" and cycle == "D":
                raise ResolutionError(f"Table '{stat_code}' is daily (D) by default, so yoy is not available. Specify cycle='M'.")
            codes = [c.strip() if c and c.strip() else "" for c in (item_codes or [])]
            while codes and not codes[-1]:
                codes.pop()
            return [
                ResolvedSource(
                    provider="ECOS",
                    dataflow=stat_code.strip(),
                    key=".".join(codes),
                    freq=cycle,
                    country=get_country("KR"),
                    transform=explicit_transform,
                    changes_only=bool(changes_only),
                    label=label,
                )
            ]
        if dataflow and dataflow.strip():
            if provider not in (*SOURCES, "WB"):
                raise ResolutionError(f"With dataflow, set source to one of {', '.join((*SOURCES, 'WB'))}.")
            if not freq:
                raise ResolutionError("Direct SDMX queries need cycle (the frequency determines the period format).")
            if not (key or "").strip() or key.strip().lower() == "all":
                raise ResolutionError(
                    "Direct SDMX queries need a key (a whole dataflow is very large and hits rate limits). "
                    "See key_template in get_metadata(source=..., dataflow=...)."
                )
            ctry = get_country(country) if country else None
            return [
                ResolvedSource(
                    provider=provider,
                    dataflow=dataflow.strip(),
                    key=(key or "").strip(),
                    freq=freq,
                    country=ctry,
                    transform=explicit_transform,
                    changes_only=bool(changes_only),
                    label=label,
                )
            ]
        raise ResolutionError(
            "Specify indicator (a concept), stat_code (an ECOS table) or source + dataflow (OECD, IMF, BIS). "
            "search_statistics finds them."
        )

    # ── Loading ─────────────────────────────────────────────────────

    async def _fetch(self, src: ResolvedSource, start: str, end: str, **ecos_kw: Any) -> list[CanonicalSeries]:
        request = SeriesRequest(
            src.provider, src.dataflow, src.key, src.freq, start, end, **ecos_kw,
            ref_area=src.country.iso2 if src.country else None,
        )
        series_list = await self.providers[src.provider].fetch(request)
        if src.mapping and src.mapping.invert:
            for series in series_list:
                invert(series, src.mapping.unit)
        return series_list

    async def load(
        self,
        src: ResolvedSource,
        start: str,
        end: str,
        *,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 1000,
        prefer_latest: bool = True,
        record: bool = True,
    ) -> LoadedSeries:
        ecos_kw = {"language": language, "start_count": start_count, "end_count": end_count, "prefer_latest": prefer_latest} if src.provider == "ECOS" else {}
        series_list = await self._fetch(src, start, end, **ecos_kw)

        if src.transform and series_list:
            lookback = transform_lookback(src.freq, src.transform)
            for series in series_list:
                if not series.observations:
                    continue
                first, last = series.observations[0].period, series.observations[-1].period
                base_last = shift(first, src.freq, -1)
                if src.transform == "yoy":
                    base_last = min(base_last, shift(last, src.freq, -lookback), key=lambda p: period_index(p, src.freq))
                try:
                    base_series = await self._fetch(
                        replace(src, key=series.series_key.split(".", 1)[1] if src.provider == "ECOS" and "." in series.series_key else src.key),
                        shift(first, src.freq, -lookback),
                        base_last,
                        **({**ecos_kw, "start_count": 1, "end_count": 1000, "prefer_latest": True} if src.provider == "ECOS" else {}),
                    )
                except ProviderError as e:
                    base_series = []
                    series.notes.append(f"Base-period data could not be retrieved, so the first {lookback} changes are empty: {e}")
                base = next((b for b in base_series if b.series_key == series.series_key), None)
                apply_transform(series, src.transform, base.observations if base else [])

        expectation = src.expectation(start, end)
        run_id = uuid.uuid4().hex[:12]
        reports = []
        for series in series_list:
            series.concept_id = src.concept.id if src.concept else None
            if series.adjustment is None and src.mapping and src.mapping.adjustment:
                series.adjustment = src.mapping.adjustment
            if series.unit is None and src.mapping and not src.transform:
                series.provenance.transformations.append(f"unit {src.mapping.unit}: not published by the provider, declared in the catalog")
            report = validate_series(series, expectation, self.revisions)
            reports.append(report)
            if record:
                self.ledger.append(report_records(report, series, run_id))
        return LoadedSeries(src, series_list, reports, start, end)

    async def load_first(self, candidates: list[ResolvedSource], start_end: Any, **kw: Any) -> LoadedSeries:
        """Try candidates in priority order; the first with data wins. start_end(src) → (start, end)."""
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for src in candidates:
            start, end = start_end(src)
            try:
                loaded = await self.load(src, start, end, **kw)
            except (ProviderError, ResolutionError) as e:
                attempts.append({**src.describe(), "result": f"error: {e}"})
                last_error = e
                continue
            if loaded.series:
                loaded.attempts = attempts
                return loaded
            attempts.append({**src.describe(), "result": f"no data for {start}–{end}"})
        if last_error and len(attempts) == 1:
            raise last_error
        lines = "; ".join(f"{a['provider']} {a['dataflow']} [{a['key']}] → {a['result']}" for a in attempts)
        raise ResolutionError(f"No source returned data. {lines}. Check the dates and the country.")

    async def cross_check(
        self,
        concept: Concept,
        country: Country,
        start_end: Any,
        *,
        freq: str | None = None,
        providers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Load every source of a concept for one country and cross-validate them."""
        mappings = concept.sources_for(country)
        if providers:
            wanted = {p.upper() for p in providers}
            mappings = [m for m in mappings if m.provider in wanted]
        # One mapping per provider, preferring the requested or the most common frequency.
        freqs = [m.freq for m in mappings]
        # Most common frequency; ties go to the one listed first (catalog priority), never daily.
        target = freq or max(dict.fromkeys(freqs), key=lambda f: (freqs.count(f), f != "D", -freqs.index(f))) if freqs else None
        chosen: dict[str, SourceMapping] = {}
        for m in sorted(mappings, key=lambda m: m.freq != target):
            chosen.setdefault(m.provider, m)
        if len(chosen) < 2:
            return {
                "status": "not_enough_sources",
                "validation_status": NOT_COMPARED,
                "concept_id": concept.id,
                "country": country.iso2,
                "sources": [m.provider for m in chosen.values()],
                "records": [],
            }
        resolved = [
            ResolvedSource(m.provider, m.dataflow, m.render_key(country), m.freq, concept, country, m, m.transform, False, concept.name_en)
            for m in chosen.values()
        ]

        async def load(src: ResolvedSource) -> LoadedSeries | dict[str, Any]:
            start, end = start_end(src)
            try:
                return await self.load(src, start, end)
            except (ProviderError, ResolutionError) as e:
                return {**src.describe(), "error": str(e)}

        results = await asyncio.gather(*(load(r) for r in resolved))
        loaded = [r for r in results if isinstance(r, LoadedSeries) and r.series]
        errors = [r for r in results if isinstance(r, dict)]

        def compare(group: list[LoadedSeries]) -> dict[str, Any]:
            return cross_validate(
                [ls.series[0] for ls in group],
                concept_id=concept.id,
                country=country.iso2,
                unit=concept.unit,
                aggregation=concept.aggregation,
                rebase=concept.compare_rebased,
                ledger=self.ledger,
            )

        # Compare at the most common frequency; sources published only at another frequency
        # (e.g. annual World Bank data next to quarterly sources) are compared with the
        # reference separately, so a finer comparison is never collapsed to annual points.
        main = [ls for ls in loaded if ls.source.freq == target] or loaded[:1]
        others = [ls for ls in loaded if ls not in main]
        groups = [main] if len(main) >= 2 else []
        groups += [[main[0], *(ls for ls in others if ls.source.freq == f)] for f in dict.fromkeys(ls.source.freq for ls in others)]
        if not groups:
            groups = [loaded]
        results_by_group = [compare(g) for g in groups]
        result, extra = results_by_group[0], []
        for extra_result in results_by_group[1:]:
            extra.append({k: extra_result.get(k) for k in ("frequency", "validation_status", "status", "method", "notes", "reason")})
            result.setdefault("agreement", {}).update(extra_result.get("agreement") or {})
        if extra:
            result["other_frequencies"] = extra
            statuses = [s for s in (result.get("validation_status"), *(e["validation_status"] for e in extra)) if s in SEVERITY]
            if statuses:
                result["validation_status"] = max(statuses, key=SEVERITY.__getitem__)
        result["source_validation"] = {ls.source.provider: ls.reports[0].compact() for ls in loaded if ls.reports}
        result["provenance"] = [ls.series[0].provenance.citation(ls.series[0].title) for ls in loaded]
        if errors:
            result["errors"] = errors
        return result
