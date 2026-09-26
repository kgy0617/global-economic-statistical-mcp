"""ECOS (Bank of Korea) adapter: StatisticSearch rows → CanonicalSeries.

The Bank of Korea has no SDMX endpoint, so ECOS is reached through its own REST API
(:mod:`global_economic_statistical_mcp.ecos_client`) and normalised here.
"""

from __future__ import annotations

import re

from global_economic_statistical_mcp.ecos_client import EcosApiError, EcosClient
from global_economic_statistical_mcp.model import (
    CanonicalSeries,
    Observation,
    Provenance,
    ecos_to_canonical,
    now_kst_iso,
    to_ecos_period,
)
from global_economic_statistical_mcp.providers.base import ProviderError, SeriesRequest
from global_economic_statistical_mcp.timeseries import (
    group_series,
    series_key,
    series_label,
    to_number,
)

AGENCY = "한국은행 (Bank of Korea)"
ATTRIBUTION = "한국은행 경제통계시스템(ECOS) Open API"
WEB_URL = "https://ecos.bok.or.kr/"

# ECOS unit label → (canonical unit, unit_mult)
_UNIT_LABELS: dict[str, tuple[str, int]] = {
    "연%": ("PC_PA", 0),
    "%": ("PC", 0),
    "%p": ("PC", 0),
    "원": ("XDC", 0),
    "천원": ("XDC", 3),
    "백만원": ("XDC", 6),
    "십억원": ("XDC", 9),
    "조원": ("XDC", 12),
    "달러": ("USD", 0),
    "천달러": ("USD", 3),
    "백만달러": ("USD", 6),
    "억달러": ("USD", 8),
    "명": ("PS", 0),
    "천명": ("PS", 3),
    "만명": ("PS", 4),
}
_INDEX_BASE = re.compile(r"^\s*([\d.]+)\s*=\s*100\s*$")


def ecos_unit(label: str | None) -> tuple[str | None, int, str | None]:
    """Map an ECOS UNIT_NAME to (unit, unit_mult, base_period)."""
    if not label:
        return None, 0, None
    text = label.strip()
    base = _INDEX_BASE.match(text)
    if base:
        return "IX", 0, base.group(1).replace(".", "")
    if text in _UNIT_LABELS:
        unit, mult = _UNIT_LABELS[text]
        return unit, mult, None
    return None, 0, None


class EcosProvider:
    id = "ECOS"
    name = "Bank of Korea ECOS"

    def __init__(self, client: EcosClient | None = None) -> None:
        self.client = client or EcosClient()

    async def close(self) -> None:
        await self.client.close()

    def _query_url(self, request: SeriesRequest) -> str:
        items = [c for c in request.key.split(".")] if request.key else []
        url = self.client._build_url(
            "StatisticSearch",
            "{API_KEY}",
            "json",
            request.language,
            str(request.start_count),
            str(request.end_count),
            request.dataflow,
            request.freq,
            to_ecos_period(request.start, request.freq),
            to_ecos_period(request.end, request.freq),
            *[c or "?" for c in items],
        )
        return url.replace("%7BAPI_KEY%7D", "{API_KEY}")

    async def fetch(self, request: SeriesRequest) -> list[CanonicalSeries]:
        items = [c or None for c in request.key.split(".")] if request.key else []
        items = (items + [None] * 4)[:4]
        try:
            res = await self.client.search_statistics(
                stat_code=request.dataflow,
                cycle=request.freq,
                start_date=to_ecos_period(request.start, request.freq),
                end_date=to_ecos_period(request.end, request.freq),
                item_code1=items[0],
                item_code2=items[1],
                item_code3=items[2],
                item_code4=items[3],
                language=request.language,
                start_count=request.start_count,
                end_count=request.end_count,
                prefer_latest=request.prefer_latest,
            )
        except EcosApiError as e:
            raise ProviderError("ECOS", e.code, str(e)) from e

        retrieved = now_kst_iso()
        out: list[CanonicalSeries] = []
        for rows in group_series(res["rows"]):
            first = rows[0]
            codes = [c for c in series_key(first) if c]
            unit, mult, base = ecos_unit(first.get("UNIT_NAME"))
            key = ".".join(codes)
            provenance = Provenance(
                provider="ECOS",
                agency=AGENCY,
                dataflow=request.dataflow,
                dataflow_name=first.get("STAT_NAME"),
                series_key=f"{request.freq}.{key}" if key else request.freq,
                retrieved_at=retrieved,
                query_url=self._query_url(request),
                web_url=WEB_URL,
                attribution=ATTRIBUTION,
            )
            out.append(
                CanonicalSeries(
                    provider="ECOS",
                    dataflow=request.dataflow,
                    series_key=provenance.series_key,
                    freq=request.freq,
                    title=f"{first.get('STAT_NAME', request.dataflow)} — {series_label(first)}",
                    observations=[
                        Observation(
                            period=ecos_to_canonical(str(r["TIME"]), request.freq),
                            value=v if isinstance(v := to_number(r.get("DATA_VALUE")), int | float) else None,
                        )
                        for r in rows
                    ],
                    provenance=provenance,
                    ref_area="KR",
                    unit=unit,
                    unit_label=first.get("UNIT_NAME"),
                    unit_mult=mult,
                    base_period=base,
                    dimensions={
                        "STAT_CODE": request.dataflow,
                        "FREQ": request.freq,
                        **{f"ITEM_CODE{i}": c for i, c in enumerate(codes, start=1)},
                    },
                    notes=[res["note"]] if res.get("note") else [],
                    truncated=bool(res.get("truncated")),
                    total_count=res.get("total_count"),
                )
            )
        return out
