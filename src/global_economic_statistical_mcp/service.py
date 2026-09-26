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
from global_economic_statistical_mcp.catalog.countries import Country, get_country
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
from global_economic_statistical_mcp.providers.ecos import EcosProvider
from global_economic_statistical_mcp.providers.sdmx_rest import (
    SOURCES,
    SdmxHttp,
    SdmxProvider,
)
from global_economic_statistical_mcp.storage import RevisionStore, ValidationLedger
from global_economic_statistical_mcp.validation import (
    Expectation,
    ValidationReport,
    cross_validate,
    report_records,
    validate_series,
)

PROVIDERS = ("ECOS", *SOURCES)
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
            raise ResolutionError("일간(D) 시계열에는 transform='yoy'를 쓸 수 없습니다. 'pop'을 쓰거나 월간 지표를 사용하세요.")
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
    label = "전년동기대비 증감률(%)" if transform == "yoy" else "전기대비 증감률(%)"
    series.title = f"{series.title} [{label}]"
    series.provenance.transformations.append(
        f"{transform}: 이 서버가 원자료 수준값에서 {label}을 계산(원값은 source_value)"
    )


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
            listed = ", ".join(f"{c.id}({c.name_ko})" for _, c in ranked[:6])
            raise ResolutionError(f"'{indicator}'에 해당하는 개념이 여러 개입니다: {listed}. concept_id나 더 구체적인 표현을 쓰세요.")
        raise ResolutionError(
            f"'{indicator}'에 해당하는 표준 개념이 없습니다. search_statistics로 개념·ECOS 통계표·SDMX 데이터플로를 찾아보세요."
        )

    def resolve_country(self, country: str | None) -> Country:
        c = get_country(country or "KR")
        if not c:
            raise ResolutionError(f"알 수 없는 국가입니다: '{country}'. ISO 코드(KR, US, JP, KOR, USA 등)를 사용하세요.")
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
        provider = source.strip().upper() if source else None
        if provider and provider not in PROVIDERS:
            raise ResolutionError(f"지원하지 않는 source입니다: '{source}'. {', '.join(PROVIDERS)} 중 하나를 쓰세요.")
        freq = freq.strip().upper() if freq else None
        if freq and freq not in VALID_CYCLES:
            raise ResolutionError(f"유효하지 않은 주기입니다: '{freq}'. A, S, Q, M, SM, D 중 하나를 쓰세요.")
        transform = (transform or "").strip().lower() or None
        if transform in ("none", "raw"):
            transform = "none"
        if transform not in (None, "none", "yoy", "pop"):
            raise ResolutionError(f"지원하지 않는 transform입니다: '{transform}'. 'yoy', 'pop', 'none' 중 하나를 쓰세요.")

        if indicator and indicator.strip():
            if stat_code or dataflow:
                raise ResolutionError("indicator와 stat_code/dataflow는 함께 쓸 수 없습니다. 하나만 지정하세요.")
            if item_codes and any(item_codes):
                raise ResolutionError("indicator를 쓸 때는 item_code를 지정할 수 없습니다. 직접 조회하려면 stat_code를 쓰세요.")
            concept = self.resolve_concept(indicator)
            ctry = self.resolve_country(country)
            mappings = concept.sources_for(ctry, provider=provider, freq=freq)
            if not mappings:
                available = sorted({(s.provider, s.freq) for s in concept.sources_for(ctry)})
                everywhere = sorted({(s.provider, s.freq) for s in concept.sources})
                raise ResolutionError(
                    f"{concept.id}({concept.name_ko})는 {ctry.iso2}"
                    + (f"·{provider}" if provider else "")
                    + (f"·주기 {freq}" if freq else "")
                    + f" 조합의 출처가 없습니다. {ctry.iso2}에서 가능한 (출처, 주기): {available or '없음'}; "
                    f"전체: {everywhere}"
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
                    label=label or concept.name_ko,
                )
                for m in mappings
            ]

        explicit_transform = None if transform in (None, "none") else transform
        if stat_code and stat_code.strip():
            if provider not in (None, "ECOS"):
                raise ResolutionError("stat_code는 ECOS 통계표코드입니다. 국제기구 데이터는 dataflow와 key를 쓰세요.")
            cycle = freq or default_ecos_cycle
            if not cycle:
                raise ResolutionError(f"통계표 '{stat_code}'의 주기를 알 수 없습니다. cycle을 지정하세요.")
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
            if provider not in SOURCES:
                raise ResolutionError("dataflow를 쓸 때는 source를 OECD, IMF, BIS 중 하나로 지정하세요.")
            if not freq:
                raise ResolutionError("SDMX 직접 조회에는 cycle(주기)을 지정하세요 (시점 형식을 맞추는 데 필요).")
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
            "indicator(표준 개념), stat_code(ECOS 통계표) 또는 source+dataflow(OECD·IMF·BIS) 중 하나가 필요합니다. "
            "search_statistics로 찾을 수 있습니다."
        )

    # ── Loading ─────────────────────────────────────────────────────

    async def _fetch(self, src: ResolvedSource, start: str, end: str, **ecos_kw: Any) -> list[CanonicalSeries]:
        request = SeriesRequest(src.provider, src.dataflow, src.key, src.freq, start, end, **ecos_kw)
        return await self.providers[src.provider].fetch(request)

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
                base_series = await self._fetch(
                    replace(src, key=series.series_key.split(".", 1)[1] if src.provider == "ECOS" and "." in series.series_key else src.key),
                    shift(first, src.freq, -lookback),
                    base_last,
                    **({**ecos_kw, "start_count": 1, "end_count": 1000, "prefer_latest": True} if src.provider == "ECOS" else {}),
                )
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
                series.provenance.transformations.append(f"단위 {src.mapping.unit}: 공급자 미표기, 카탈로그 선언값")
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
            attempts.append({**src.describe(), "result": "no data"})
        if last_error and len(attempts) == 1:
            raise last_error
        raise ResolutionError(f"모든 출처에서 데이터를 얻지 못했습니다: {attempts}")

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
        target = freq or max(set(freqs), key=lambda f: (freqs.count(f), f != "D")) if freqs else None
        chosen: dict[str, SourceMapping] = {}
        for m in sorted(mappings, key=lambda m: m.freq != target):
            chosen.setdefault(m.provider, m)
        if len(chosen) < 2:
            return {
                "status": "not_enough_sources",
                "concept_id": concept.id,
                "country": country.iso2,
                "sources": [m.provider for m in chosen.values()],
                "records": [],
            }
        resolved = [
            ResolvedSource(m.provider, m.dataflow, m.render_key(country), m.freq, concept, country, m, m.transform, False, concept.name_ko)
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
        result = cross_validate(
            [ls.series[0] for ls in loaded],
            concept_id=concept.id,
            country=country.iso2,
            unit=concept.unit,
            ledger=self.ledger,
        )
        result["source_validation"] = {ls.source.provider: ls.reports[0].compact() for ls in loaded if ls.reports}
        result["provenance"] = [ls.series[0].provenance.citation(ls.series[0].title) for ls in loaded]
        if errors:
            result["errors"] = errors
        return result
