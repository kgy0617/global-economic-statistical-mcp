"""World Bank provider: the Data360 API (https://data360api.worldbank.org), not SDMX.

The World Bank's own MCP server (github.com/worldbank/data360-mcp) is built on the same API;
this adapter calls the API directly so World Bank series enter the same Canonical Model,
validation and provenance as every other provider, and the LLM never needs a second MCP.

Endpoints used (verified against the live API):

- ``GET  /data360/data``      observations: DATABASE_ID, INDICATOR, REF_AREA (comma list),
                              timePeriodFrom/To, skip/top — flat SDMX-style rows
- ``POST /data360/metadata``  indicator name, definition, unit and periodicity
- ``POST /data360/searchv2``  indicator search

A series is addressed like an SDMX one: dataflow = database id (``WB_WDI``) and
key = ``INDICATOR.REF_AREA`` (e.g. ``WB_WDI_SP_POP_TOTL.USA``; several areas with ``+``).
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from global_economic_statistical_mcp.catalog.countries import iso2
from global_economic_statistical_mcp.model import (
    CanonicalSeries,
    Observation,
    Provenance,
    now_kst_iso,
    parse_period,
)
from global_economic_statistical_mcp.providers.base import ProviderError, SeriesRequest
from global_economic_statistical_mcp.providers.sdmx_rest import SdmxHttp, _number

API = "https://data360api.worldbank.org/data360"
WEB = "https://data360.worldbank.org"
AGENCY = "World Bank"
ATTRIBUTION = "World Bank Data360 (https://data360.worldbank.org/)"
_PAGE = 1000
_DISAGGREGATION = ("SEX", "AGE", "URBANISATION", "COMP_BREAKDOWN_1", "COMP_BREAKDOWN_2", "COMP_BREAKDOWN_3")

# Data360 unit codes → canonical units. PC_A is the annual percentage change of growth and
# inflation indicators.
_UNITS = {
    "PC_A": "PC_YOY",
    "XDC_K": "XDC",
    "XDC": "XDC",
    "XDC_USD": "XDC_USD",
    "USD": "USD",
    "PT_GDP": "PC_GDP",
    "PPP_K_2021": "USD_PPP",
    "PS": "PS",
}


def split_key(key: str) -> tuple[str, list[str]]:
    """'WB_WDI_SP_POP_TOTL.USA+KOR' → ('WB_WDI_SP_POP_TOTL', ['USA', 'KOR'])."""
    indicator, _, areas = (key or "").partition(".")
    if not indicator:
        raise ProviderError("WB", "BAD_KEY", "World Bank keys are INDICATOR.REF_AREA, e.g. 'WB_WDI_SP_POP_TOTL.USA'")
    return indicator, [a for a in areas.replace(",", "+").split("+") if a]


class Data360Provider:
    id = "WB"
    name = "World Bank Data360"

    def __init__(self, http: SdmxHttp, api: str = API) -> None:
        self.http = http
        self.api = api.rstrip("/")

    async def close(self) -> None:  # the shared SdmxHttp is closed by its owner
        return None

    async def fetch(self, request: SeriesRequest) -> list[CanonicalSeries]:
        indicator, areas = split_key(request.key)
        params = {"DATABASE_ID": request.dataflow, "INDICATOR": indicator}
        if areas:
            params["REF_AREA"] = ",".join(areas)
        if request.start:
            params["timePeriodFrom"] = request.start[:4]
        if request.end:
            params["timePeriodTo"] = request.end[:4]
        rows: list[dict[str, Any]] = []
        url = f"{self.api}/data?{urllib.parse.urlencode(params)}"
        while True:
            page = await self.http.get_json(self.id, f"{url}&skip={len(rows)}&top={_PAGE}", "application/json", ttl=15 * 60)
            batch = (page or {}).get("value") or []
            rows.extend(batch)
            if len(batch) < _PAGE or len(rows) >= int((page or {}).get("count") or 0):
                break
        if not rows:
            return []
        meta = await self.metadata(indicator)
        return self._series(rows, request, indicator, meta, url)

    def _series(
        self, rows: list[dict[str, Any]], request: SeriesRequest, indicator: str, meta: dict[str, Any], url: str
    ) -> list[CanonicalSeries]:
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for row in rows:
            key = (row.get("INDICATOR") or indicator, row.get("REF_AREA") or "", *(str(row.get(d) or "_Z") for d in _DISAGGREGATION))
            groups.setdefault(key, []).append(row)
        retrieved = now_kst_iso()
        out = []
        for key, group in groups.items():
            first = group[0]
            freq = first.get("FREQ") or request.freq
            observations = []
            for row in group:
                try:
                    _, period = parse_period(str(row.get("TIME_PERIOD")), freq)
                except ValueError:
                    period = str(row.get("TIME_PERIOD"))
                observations.append(Observation(period=period, value=_number(row.get("OBS_VALUE")), status=row.get("OBS_STATUS")))
            observations.sort(key=lambda o: o.period)
            series_key = f"{key[0]}.{key[1]}"
            disaggregated = [f"{d}={v}" for d, v in zip(_DISAGGREGATION, key[2:]) if v not in ("_T", "_Z")]
            if disaggregated:
                series_key += "[" + ",".join(disaggregated) + "]"
            unit_code = first.get("UNIT_MEASURE")
            mult = first.get("UNIT_MULT")
            title = meta.get("name") or key[0]
            out.append(
                CanonicalSeries(
                    provider=self.id,
                    dataflow=request.dataflow,
                    series_key=series_key,
                    freq=freq,
                    title=f"{title} — {key[1]}" if key[1] else title,
                    observations=observations,
                    provenance=Provenance(
                        provider=self.id,
                        agency=AGENCY,
                        dataflow=request.dataflow,
                        dataflow_name=meta.get("database_name") or request.dataflow,
                        series_key=series_key,
                        retrieved_at=retrieved,
                        query_url=url,
                        web_url=f"{WEB}/en/indicator/{key[0]}",
                        attribution=ATTRIBUTION,
                    ),
                    ref_area=iso2(key[1]) if key[1] else None,
                    unit=_UNITS.get(unit_code or "", unit_code),
                    unit_label=meta.get("measurement_unit"),
                    unit_mult=int(mult) if str(mult or "").lstrip("-").isdigit() else 0,
                    dimensions={"INDICATOR": key[0], "REF_AREA": key[1], **dict(zip(_DISAGGREGATION, key[2:]))},
                )
            )
        return out

    async def metadata(self, indicator: str) -> dict[str, Any]:
        """Name, definition, unit and periodicity of an indicator ({} if unavailable)."""
        body = {
            "query": f"series_description/idno eq '{indicator.replace(chr(39), '')}'",
            "select": "series_description/idno, series_description/name, series_description/database_id, "
            "series_description/definition_short, series_description/measurement_unit, series_description/periodicity",
        }
        try:
            doc = await self.http.post_json(self.id, f"{self.api}/metadata", body, ttl=6 * 60 * 60)
        except ProviderError:
            return {}
        values = (doc or {}).get("value") or []
        return (values[0].get("series_description") or {}) if values else {}

    async def structure(self, indicator: str) -> dict[str, Any]:
        """Indicator metadata shaped like an SDMX structure summary (for get_metadata)."""
        indicator = indicator.strip().split(".")[0]
        meta = await self.metadata(indicator)
        if not meta:
            raise ProviderError(self.id, "NOT_FOUND", f"No World Bank indicator '{indicator}'. Find ids with search_statistics.")
        return {
            "dataflow": meta.get("database_id"),
            "indicator": indicator,
            "name": meta.get("name"),
            "description": meta.get("definition_short"),
            "unit": meta.get("measurement_unit"),
            "periodicity": meta.get("periodicity"),
            "dimensions": [
                {"id": "INDICATOR", "name": "Indicator", "codes": [{"code": indicator, "name": meta.get("name")}]},
                {"id": "REF_AREA", "name": "Reference area (ISO3; EMU for the euro area)", "codes": []},
            ],
            "structure_url": f"{WEB}/en/indicator/{indicator}",
        }

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        body = {
            "search": query,
            "top": max(1, min(limit, 50)),
            "filter": "type eq 'indicator'",
            "select": "series_description/idno, series_description/name, series_description/database_id",
        }
        doc = await self.http.post_json(self.id, f"{self.api}/searchv2", body, ttl=60 * 60)
        out = []
        for item in (doc or {}).get("value") or []:
            meta = item.get("series_description") or {}
            if meta.get("idno"):
                out.append({"provider": self.id, "dataflow": meta.get("database_id"), "indicator": meta["idno"], "name": meta.get("name")})
        return out
