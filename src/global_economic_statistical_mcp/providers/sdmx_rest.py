"""Generic SDMX REST adapter for OECD, IMF and BIS.

The three agencies speak different dialects, all verified against the live APIs:

========  ===========================================  ========  ====================
Provider  Endpoint                                     SDMX API  Data format
========  ===========================================  ========  ====================
OECD      https://sdmx.oecd.org/public/rest            2.1       SDMX-JSON 2.0
IMF       https://api.imf.org/external/sdmx/3.0        3.0       SDMX-JSON 2.0
BIS       https://stats.bis.org/api/v1 (data) / v2     2.1/3.0   SDMX-JSON 1.0 / 2.0
========  ===========================================  ========  ====================

Data is always requested with ``dimensionAtObservation=AllDimensions`` so one flat parser
handles every dialect. Results are post-filtered against the requested key because at
least one provider (IMF) silently ignores the whole key when it contains a wildcard.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from global_economic_statistical_mcp.catalog.countries import iso2
from global_economic_statistical_mcp.catalog.search import dataflow_name
from global_economic_statistical_mcp.model import (
    CanonicalSeries,
    Observation,
    Provenance,
    canonical_unit,
    now_kst_iso,
    parse_period,
)
from global_economic_statistical_mcp.providers.base import ProviderError, SeriesRequest

JSON1 = "application/vnd.sdmx.data+json;version=1.0.0"
JSON2 = "application/vnd.sdmx.data+json;version=2.0.0"
STRUCT1 = "application/vnd.sdmx.structure+json;version=1.0"
STRUCT2 = "application/vnd.sdmx.structure+json;version=2.0.0"


@dataclass(frozen=True)
class SdmxSource:
    id: str
    name: str
    agency: str
    api: str  # "2.1" or "3.0"
    data_base: str
    structure_base: str
    structure_api: str
    data_accept: str
    structure_accept: str
    web_url: str
    attribution: str


SOURCES: dict[str, SdmxSource] = {
    "OECD": SdmxSource(
        id="OECD",
        name="OECD Data Explorer",
        agency="Organisation for Economic Co-operation and Development (OECD)",
        api="2.1",
        data_base="https://sdmx.oecd.org/public/rest",
        structure_base="https://sdmx.oecd.org/public/rest",
        structure_api="2.1",
        data_accept=JSON2,
        structure_accept=STRUCT1,
        web_url="https://data-explorer.oecd.org/",
        attribution="OECD Data Explorer (https://data-explorer.oecd.org/)",
    ),
    "IMF": SdmxSource(
        id="IMF",
        name="IMF Data",
        agency="International Monetary Fund (IMF)",
        api="3.0",
        data_base="https://api.imf.org/external/sdmx/3.0",
        structure_base="https://api.imf.org/external/sdmx/3.0",
        structure_api="3.0",
        data_accept=JSON2,
        structure_accept=STRUCT2,
        web_url="https://data.imf.org/",
        attribution="IMF Data (https://data.imf.org/)",
    ),
    "BIS": SdmxSource(
        id="BIS",
        name="BIS Data Portal",
        agency="Bank for International Settlements (BIS)",
        api="2.1",
        data_base="https://stats.bis.org/api/v1",
        structure_base="https://stats.bis.org/api/v2",
        structure_api="3.0",
        data_accept=JSON1,
        structure_accept=STRUCT2,
        web_url="https://data.bis.org/",
        attribution="BIS Data Portal (https://data.bis.org/)",
    ),
}

_FLOW_REF = re.compile(r"^(?P<agency>[A-Za-z0-9_.\-]+):(?P<id>[A-Za-z0-9_@.\-]+)(?:\((?P<version>[0-9A-Za-z.+*~]+)\))?$")

# Dimension / attribute ids that carry canonical metadata, across dialects.
_AREA_DIMS = ("REF_AREA", "COUNTRY")
_FREQ_DIMS = ("FREQ", "FREQUENCY")
_BASE_ATTRS = ("BASE_PER", "REFERENCE_PERIOD", "BASE_PERIOD")
_STATUS_ATTRS = ("OBS_STATUS", "STATUS")

# Provider codes that imply a unit when no UNIT_MEASURE is published.
_IMPLIED_UNITS: dict[str, tuple[str, str | None]] = {
    "IX": ("IX", None),
    "YOY_PCH_PA_PT": ("PC_YOY", None),
    "POP_PCH_PA_PT": ("PC_POP", None),
    "XDC_USD": ("XDC_USD", None),
    "XDC": ("XDC", None),  # IMF TYPE_OF_TRANSFORMATION / UNIT: domestic currency
    "USD": ("USD", None),
    "628": ("IX", "2010"),  # BIS: Index, 2010 = 100
    "771": ("PC_YOY", None),  # BIS: Year-on-year changes, in per cent
}


_TRANSFORMATION_UNITS = {"GY": "PC_YOY_T", "G1": "PC_POP_T"}
# SDMX ADJUSTMENT codes → SA / NSA ("_Z" = not applicable)
_ADJUSTMENT = {"Y": "SA", "S": "SA", "T": "SA", "N": "NSA"}
_IMPLIED_UNITS.update({"PC_YOY_T": ("PC_YOY", None), "PC_POP_T": ("PC_POP", None)})


def parse_flow_ref(ref: str) -> tuple[str, str, str]:
    """'OECD.SDD.TPS:DSD_PRICES@DF_PRICES_ALL(1.0)' → (agency, id, version)."""
    match = _FLOW_REF.match(ref.strip())
    if not match:
        raise ProviderError("SDMX", "BAD_DATAFLOW", f"Invalid dataflow reference '{ref}' (expected e.g. 'BIS:WS_CBPOL(1.0)')")
    return match["agency"], match["id"], match["version"] or "latest"


def _text(value: Any) -> str | None:
    """SDMX names can be plain strings or {lang: text} maps."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("en") or value.get("ko") or next(iter(value.values()), None)
    return str(value)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):  # "NaN" is how some providers publish a missing value
        return None
    return int(number) if number.is_integer() and "." not in str(value) else number


def data_url(source: SdmxSource, flow_ref: str, key: str, start: str | None, end: str | None) -> str:
    agency, flow, version = parse_flow_ref(flow_ref)
    key = key or "all"
    if source.api == "3.0":
        version = "+" if version in ("latest", "") else version
        params = [("dimensionAtObservation", "AllDimensions")]
        bounds = "+".join(b for b in (f"ge:{start}" if start else "", f"le:{end}" if end else "") if b)
        if bounds:
            params.insert(0, ("c[TIME_PERIOD]", bounds))
        query = urllib.parse.urlencode(params, safe=":+")
        return f"{source.data_base}/data/dataflow/{agency}/{flow}/{version}/{key}?{query}"
    params = [("dimensionAtObservation", "AllDimensions")]
    if start:
        params.insert(0, ("startPeriod", start))
    if end:
        params.insert(1 if start else 0, ("endPeriod", end))
    version = "latest" if version in ("", "+") else version
    return f"{source.data_base}/data/{agency},{flow},{version}/{key}?{urllib.parse.urlencode(params)}"


def structure_url(source: SdmxSource, flow_ref: str) -> str:
    agency, flow, version = parse_flow_ref(flow_ref)
    if source.structure_api == "3.0":
        version = "+" if version in ("latest", "") else version
        return f"{source.structure_base}/structure/dataflow/{agency}/{flow}/{version}?references=all"
    return f"{source.structure_base}/dataflow/{agency}/{flow}/{version}?references=all"


# ── Parsing ─────────────────────────────────────────────────────────


def parse_data_message(doc: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], str | None]:
    """Flatten an SDMX-JSON 1.0/2.0 AllDimensions data message.

    Returns (rows, value_names, structure_name). Each row has one key per dimension,
    ``@ATTR`` keys for attributes, and ``_value``.
    """
    data = doc.get("data", doc)
    datasets = data.get("dataSets") or []
    if not datasets:
        return [], {}, None
    structure = data.get("structure") or (data.get("structures") or [{}])[datasets[0].get("structure", 0) or 0]
    dims = structure.get("dimensions", {}).get("observation") or []
    attributes = structure.get("attributes") or {}
    obs_attrs = attributes.get("observation") or []
    ds_attrs = attributes.get("dataSet") or []

    def value_id(component: dict[str, Any], index: Any) -> str | None:
        if index is None:
            return None
        if not isinstance(index, int):
            # SDMX-JSON 2.0 allows the attribute value itself instead of an index.
            return str(index)
        values = component.get("values") or []
        if not 0 <= index < len(values) or values[index] is None:
            return None
        v = values[index]
        return str(v.get("id") if v.get("id") is not None else v.get("value", v.get("name")))

    rows: list[dict[str, Any]] = []
    for dataset in datasets:
        base: dict[str, Any] = {}
        for comp, idx in zip(ds_attrs, dataset.get("attributes") or []):
            if (val := value_id(comp, idx)) is not None:
                base[f"@{comp['id']}"] = val
        for key, values in (dataset.get("observations") or {}).items():
            row = dict(base)
            for comp, idx in zip(dims, key.split(":")):
                row[comp["id"]] = value_id(comp, int(idx))
            row["_value"] = values[0] if values else None
            for comp, idx in zip(obs_attrs, values[1:]):
                if (val := value_id(comp, idx)) is not None:
                    row[f"@{comp['id']}"] = val
            rows.append(row)
    names = {
        d["id"]: {str(v.get("id", v.get("value"))): _text(v.get("names") or v.get("name")) or "" for v in d.get("values") or []}
        for d in dims
    }
    return rows, names, _text(structure.get("names") or structure.get("name"))


def _dims_match(row: dict[str, Any], dim_ids: list[str], key: str) -> bool:
    """Does a row satisfy the requested key? ('.'-separated, '+'/',' alternatives, '' or '*' wildcard)."""
    if not key or key == "all":
        return True
    for dim_id, part in zip(dim_ids, key.split(".")):
        if part in ("", "*"):
            continue
        allowed = set(re.split(r"[+,]", part))
        if str(row.get(dim_id)) not in allowed:
            return False
    return True


def rows_to_series(
    rows: list[dict[str, Any]],
    names: dict[str, dict[str, str]],
    *,
    source: SdmxSource,
    request: SeriesRequest,
    flow_name: str | None,
    query_url: str,
) -> tuple[list[CanonicalSeries], int]:
    """Group flat rows into CanonicalSeries; returns (series, rows dropped by the key filter)."""
    if not rows:
        return [], 0
    dim_ids = [k for k in rows[0] if not k.startswith("@") and k not in ("_value", "TIME_PERIOD")]
    kept = [r for r in rows if _dims_match(r, dim_ids, request.key)]
    dropped = len(rows) - len(kept)

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in kept:
        groups.setdefault(tuple(str(row.get(d)) for d in dim_ids), []).append(row)

    retrieved = now_kst_iso()
    out: list[CanonicalSeries] = []
    for key_values, group in groups.items():
        dims = dict(zip(dim_ids, key_values))
        freq = next((dims[d] for d in _FREQ_DIMS if d in dims), request.freq)
        observations = []
        for row in group:
            try:
                _, period = parse_period(str(row.get("TIME_PERIOD")), freq)
            except ValueError:
                period = str(row.get("TIME_PERIOD"))
            status = next((row[f"@{a}"] for a in _STATUS_ATTRS if f"@{a}" in row), None)
            observations.append(Observation(period=period, value=_number(row.get("_value")), status=status))
        observations.sort(key=lambda o: o.period)

        first = group[0]
        unit_code = dims.get("UNIT_MEASURE") or dims.get("UNIT") or first.get("@UNIT_MEASURE")
        unit, implied_base = None, None
        # A growth-rate transformation outranks the unit code (OECD: UNIT_MEASURE=PA + GY is a
        # year-on-year change, not an interest rate).
        for code in (
            _TRANSFORMATION_UNITS.get(dims.get("TRANSFORMATION", "")),
            unit_code,
            dims.get("TYPE_OF_TRANSFORMATION"),
            dims.get("INDICATOR"),
        ):
            if code and code in _IMPLIED_UNITS:
                unit, implied_base = _IMPLIED_UNITS[code]
                break
        if unit is None and unit_code:
            unit = canonical_unit(unit_code)
        base = next((first[f"@{a}"] for a in _BASE_ATTRS if f"@{a}" in first), None) or implied_base
        if base:
            base = re.sub(r"[^0-9]", "", str(base))[:6] or None
        mult = first.get("@UNIT_MULT")
        area = next((dims[d] for d in _AREA_DIMS if d in dims), None)

        labels = [
            names.get(d, {}).get(v) or v
            for d, v in dims.items()
            if d not in _FREQ_DIMS and v not in ("_Z", "_X", "_T")
        ]
        series_key = ".".join(key_values)
        out.append(
            CanonicalSeries(
                provider=source.id,
                dataflow=request.dataflow,
                series_key=series_key,
                freq=freq,
                title=" — ".join(x for x in [flow_name, ", ".join(l for l in labels if l)] if x),
                observations=observations,
                provenance=Provenance(
                    provider=source.id,
                    agency=source.agency,
                    dataflow=request.dataflow,
                    dataflow_name=flow_name,
                    series_key=series_key,
                    retrieved_at=retrieved,
                    query_url=query_url,
                    web_url=source.web_url,
                    attribution=source.attribution,
                ),
                ref_area=iso2(area),
                unit=unit,
                unit_label=(names.get("UNIT_MEASURE") or names.get("UNIT") or {}).get(unit_code) if unit_code else None,
                unit_mult=int(mult) if mult not in (None, "") and str(mult).lstrip("-").isdigit() else 0,
                base_period=base,
                adjustment=_ADJUSTMENT.get(dims.get("ADJUSTMENT", ""), None),
                dimensions=dims,
            )
        )
    return out, dropped


# ── HTTP ────────────────────────────────────────────────────────────


# Concurrent requests allowed per provider. OECD enforces a strict per-IP quota, so it gets one.
_CONCURRENCY = {"OECD": 1}
_DEFAULT_COOLDOWN_SECONDS = 60.0


class SdmxHttp:
    """Async HTTP helper: retries, a TTL cache and a per-provider rate-limit circuit breaker.

    When a provider answers 429 it is put on cooldown (``Retry-After`` or 60 s): further
    calls fail fast with ``RATE_LIMITED`` so the resolver falls back to the next source
    instead of hammering a provider that has asked us to stop.
    """

    def __init__(self, timeout: float = 60.0, max_retries: int = 2, backoff: float = 0.5) -> None:
        self._http = httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": "global-economic-statistical-mcp"})
        self.max_retries = max_retries
        self.backoff = backoff
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Semaphore] = {}
        self._cooldown_until: dict[str, float] = {}

    async def close(self) -> None:
        await self._http.aclose()

    def cooldown_remaining(self, provider: str) -> float:
        return max(0.0, self._cooldown_until.get(provider, 0.0) - time.monotonic())

    def _rate_limited(self, provider: str, seconds: float) -> ProviderError:
        return ProviderError(
            provider,
            "RATE_LIMITED",
            f"{provider} rate limit exceeded. Retry in about {int(seconds) + 1} s or use another source.",
        )

    async def get_json(self, provider: str, url: str, accept: str, ttl: float) -> Any:
        cache_key = (url, accept)
        cached = self._cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if (remaining := self.cooldown_remaining(provider)) > 0:
            raise self._rate_limited(provider, remaining)
        sem = self._locks.setdefault(provider, asyncio.Semaphore(_CONCURRENCY.get(provider, 4)))
        async with sem:
            doc = await self._get(provider, url, accept)
        if len(self._cache) > 256:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = (time.monotonic() + ttl, doc)
        return doc

    async def _get(self, provider: str, url: str, accept: str) -> Any:
        for attempt in range(self.max_retries + 1):
            if (remaining := self.cooldown_remaining(provider)) > 0:
                raise self._rate_limited(provider, remaining)
            try:
                response = await self._http.get(url, headers={"Accept": accept})
            except httpx.TimeoutException as e:
                error = ProviderError(provider, "TIMEOUT", "Request timed out")
                cause: Exception = e
            except httpx.RequestError as e:
                error = ProviderError(provider, "NETWORK_ERROR", f"Network error: {type(e).__name__}")
                cause = e
            else:
                if response.status_code == 404:
                    return None  # SDMX: NoResultsFound
                if response.status_code == 429:
                    try:
                        wait = float(response.headers.get("Retry-After", _DEFAULT_COOLDOWN_SECONDS))
                    except ValueError:
                        wait = _DEFAULT_COOLDOWN_SECONDS
                    # Quotas such as OECD's are hourly; a tiny Retry-After would only invite another 429.
                    wait = max(wait, _DEFAULT_COOLDOWN_SECONDS)
                    self._cooldown_until[provider] = time.monotonic() + wait
                    raise self._rate_limited(provider, wait)
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as e:
                        raise ProviderError(provider, "PARSE_ERROR", "Cannot parse the JSON response") from e
                error = ProviderError(provider, f"HTTP_{response.status_code}", response.text[:200].strip() or response.reason_phrase)
                cause = error
                if response.status_code < 500:
                    raise error
            if attempt >= self.max_retries:
                raise error from cause
            await asyncio.sleep(self.backoff * (2**attempt))
        raise AssertionError("unreachable")


class SdmxProvider:
    def __init__(self, source: SdmxSource, http: SdmxHttp) -> None:
        self.source = source
        self.id = source.id
        self.name = source.name
        self.http = http

    async def close(self) -> None:  # the shared SdmxHttp is closed by its owner
        return None

    async def fetch(self, request: SeriesRequest) -> list[CanonicalSeries]:
        url = data_url(self.source, request.dataflow, request.key, request.start, request.end)
        doc = await self.http.get_json(self.id, url, self.source.data_accept, ttl=15 * 60)
        if not doc:
            return []
        rows, names, flow_name = parse_data_message(doc)
        flow_name = flow_name or dataflow_name(self.id, request.dataflow)
        series, dropped = rows_to_series(
            rows, names, source=self.source, request=request, flow_name=flow_name, query_url=url
        )
        if dropped:
            note = (
                f"{self.id} returned {dropped} observations outside the requested key '{request.key}'; "
                "they were dropped (the provider ignored the key filter)."
            )
            for s in series:
                s.notes.append(note)
        return series

    async def structure(self, flow_ref: str) -> dict[str, Any]:
        url = structure_url(self.source, flow_ref)
        doc = await self.http.get_json(self.id, url, self.source.structure_accept, ttl=6 * 60 * 60)
        if not doc:
            raise ProviderError(self.id, "NOT_FOUND", f"Dataflow '{flow_ref}' not found")
        return summarize_structure(doc, flow_ref, url)


def _plain(text: str | None, limit: int = 500) -> str | None:
    """Strip HTML and shorten a provider description."""
    if not text:
        return None
    clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def summarize_structure(doc: dict[str, Any], flow_ref: str, url: str) -> dict[str, Any]:
    """DSD dimensions with their codelists, from an SDMX-JSON 1.0/2.0 structure message."""
    data = doc.get("data", doc)
    flows = data.get("dataflows") or [{}]
    dsd = (data.get("dataStructures") or [{}])[0]
    components = dsd.get("dataStructureComponents") or {}
    # References may carry exact versions ("(1.6)") or version ranges ("(1.0+.0)", IMF), so
    # artefacts are matched on agency and id only.
    codelists = {f"{c.get('agencyID')}:{c.get('id')}": c for c in data.get("codelists") or []}
    concepts: dict[str, dict[str, Any]] = {}
    for scheme in data.get("conceptSchemes") or []:
        for concept in scheme.get("concepts") or []:
            concepts[f"{scheme.get('agencyID')}:{scheme.get('id')}.{concept.get('id')}"] = concept

    def urn_key(urn: str | None) -> str:
        target = (urn or "").split("=", 1)[-1]
        return re.sub(r"\([^)]*\)", "", target)

    def concept_of(component: dict[str, Any]) -> dict[str, Any] | None:
        return concepts.get(urn_key(component.get("conceptIdentity")))

    def enumeration(component: dict[str, Any]) -> dict[str, Any] | None:
        urn = (component.get("localRepresentation") or {}).get("enumeration")
        if not urn:
            urn = ((concept_of(component) or {}).get("coreRepresentation") or {}).get("enumeration")
        return codelists.get(urn_key(urn)) if urn else None

    def concept_name(component: dict[str, Any]) -> str | None:
        concept = concept_of(component)
        return _text((concept or {}).get("names") or (concept or {}).get("name"))

    dimensions = []
    for dim in (components.get("dimensionList") or {}).get("dimensions") or []:
        codelist = enumeration(dim)
        dimensions.append(
            {
                "id": dim.get("id"),
                "position": dim.get("position"),
                "name": concept_name(dim),
                "codelist": f"{codelist.get('agencyID')}:{codelist.get('id')}" if codelist else None,
                "codes": [
                    {"code": c.get("id"), "name": _text(c.get("names") or c.get("name"))}
                    for c in (codelist or {}).get("codes") or []
                ],
            }
        )
    attributes = [
        {"id": a.get("id"), "name": concept_name(a)}
        for a in (components.get("attributeList") or {}).get("attributes") or []
    ]
    _, flow_id, _ = parse_flow_ref(flow_ref)
    flow = next((f for f in flows if f.get("id") == flow_id), flows[0])
    return {
        "dataflow": flow_ref,
        "name": _text(flow.get("names") or flow.get("name")),
        "description": _plain(_text(flow.get("descriptions") or flow.get("description"))),
        "dimensions": dimensions,
        "attributes": attributes,
        "structure_url": url,
    }
