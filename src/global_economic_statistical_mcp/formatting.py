"""Render canonical series (with provenance and validation) for LLMs and other systems.

Formats:
- ``compact`` (default): per series ``[period, value]`` rows, plus provenance and validation
- ``csv``: one row per observation; provenance and validation as ``#`` comment lines
- ``json``: the full canonical model and full validation reports
- ``sdmx``: an SDMX-JSON 2.1 data message
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any

from global_economic_statistical_mcp import ecos_sdmx
from global_economic_statistical_mcp.config import KST
from global_economic_statistical_mcp.model import (
    UNIT_LABELS,
    CanonicalSeries,
    Observation,
)
from global_economic_statistical_mcp.service import drop_unchanged
from global_economic_statistical_mcp.timeseries import dumps
from global_economic_statistical_mcp.validation import ValidationReport

FORMATS = {"compact", "csv", "json", "sdmx"}


def _has_levels(observations: list[Observation]) -> bool:
    return any(o.source_value is not None for o in observations)


def provenance_block(series: CanonicalSeries) -> dict[str, Any]:
    p = series.provenance
    out = {
        "series_id": series.series_id,
        "provider": p.provider,
        "agency": p.agency,
        "dataflow": p.dataflow,
        "series_key": p.series_key,
        "retrieved_at": p.retrieved_at,
        "citation": p.citation(series.title),
        "web_url": p.web_url,
        "query_url": p.query_url,
    }
    if p.dataflow_name:
        out["dataflow_name"] = p.dataflow_name
    if p.transformations:
        out["transformations"] = p.transformations
    return out


def series_block(series: CanonicalSeries, observations: list[Observation], declared_unit: str | None) -> dict[str, Any]:
    levels = _has_levels(observations)
    unit = series.unit or declared_unit
    out: dict[str, Any] = {
        "series_id": series.series_id,
        "title": series.title,
        "provider": series.provider,
        "country": series.ref_area,
        "freq": series.freq,
        "unit": unit,
        "unit_label": series.unit_label or UNIT_LABELS.get(unit or ""),
    }
    if series.unit is None and declared_unit:
        out["unit_source"] = "catalog"
    if series.unit_mult:
        out["unit_mult"] = series.unit_mult
    if series.base_period:
        out["base_period"] = series.base_period
    if series.adjustment:
        out["adjustment"] = series.adjustment
    out["columns"] = ["period", "value", "level"] if levels else ["period", "value"]
    out["data"] = [
        [o.period, o.value, o.source_value] if levels else [o.period, o.value] for o in observations
    ]
    if series.truncated:
        out["truncated"] = True
    if series.total_count is not None and series.provider == "ECOS":
        out["total_count"] = series.total_count
    if series.notes:
        out["notes"] = series.notes
    return out


def render(
    series_list: list[CanonicalSeries],
    reports: list[ValidationReport],
    fmt: str,
    *,
    header: dict[str, Any] | None = None,
    declared_unit: str | None = None,
    changes_only: bool = False,
    language: str = "kr",
    extra: dict[str, Any] | None = None,
) -> str:
    views = [drop_unchanged(s.observations) if changes_only else s.observations for s in series_list]
    header = {k: v for k, v in (header or {}).items() if v is not None}
    if changes_only:
        header["changes_only"] = True

    extra = extra or {}
    if fmt == "json":
        return dumps(
            {
                **header,
                "series": [s.to_dict() for s in series_list],
                "validation": [r.to_dict() for r in reports],
                **extra,
            }
        )
    if fmt == "sdmx":
        message = sdmx_data_message(series_list, views, language=language)
        cv = extra.get("cross_validation")
        if cv:
            structure = message["data"]["structures"][0]
            structure["annotations"].append(
                {"type": "CROSS_VALIDATION", "title": f"{cv.get('status')} {cv.get('counts', {})}"}
            )
            message["data"]["dataSets"][0]["annotations"] = list(range(len(structure["annotations"])))
        return dumps(message)
    if fmt == "csv":
        buffer = io.StringIO()
        for key, value in header.items():
            buffer.write(f"# {key}: {value}\n")
        for s, r in zip(series_list, reports):
            buffer.write(f"# source {s.series_id}: {s.provenance.citation(s.title)}\n")
            buffer.write(f"# validation {s.series_id}: {r.status} {r.compact().get('issues', '')}\n")
        cv = extra.get("cross_validation")
        if cv:
            buffer.write(f"# cross_validation: {cv.get('status')} {cv.get('counts', {})} method={cv.get('method')}\n")
        writer = csv.writer(buffer, lineterminator="\n")
        levels = any(_has_levels(v) for v in views)
        writer.writerow(["SERIES_ID", "PERIOD", "VALUE", *(["LEVEL"] if levels else []), "UNIT"])
        for s, obs in zip(series_list, views):
            unit = s.unit or declared_unit or ""
            for o in obs:
                row = [s.series_id, o.period, "" if o.value is None else o.value]
                if levels:
                    row.append("" if o.source_value is None else o.source_value)
                writer.writerow([*row, unit])
        return buffer.getvalue().rstrip("\n")
    return dumps(
        {
            **header,
            "series": [series_block(s, v, declared_unit) for s, v in zip(series_list, views)],
            "provenance": [provenance_block(s) for s in series_list],
            "validation": [{"series_id": r.series_id, **r.compact()} for r in reports],
            **{k: _trim_cross_validation(v) if k == "cross_validation" else v for k, v in extra.items()},
        }
    )


def _trim_cross_validation(result: dict[str, Any], keep: int = 12) -> dict[str, Any]:
    """Compact view: the latest records only (all records are in the ledger and in json format)."""
    records = result.get("records") or []
    if len(records) <= keep:
        return result
    return {**result, "records": records[-keep:], "records_omitted": len(records) - keep}


# ── SDMX-JSON 2.1 data message for canonical series ─────────────────


def _dataflow_link(series: CanonicalSeries) -> dict[str, str]:
    """A URN when the dataflow version is known, otherwise the query URL (never an invented version)."""
    if series.provider == "ECOS":
        return {"rel": "dataflow", "urn": ecos_sdmx.dataflow_urn(series.dataflow)}
    agency, rest = series.dataflow.split(":", 1)
    flow, _, version = rest.partition("(")
    version = version.rstrip(")")
    if version and version[0].isdigit():
        return {"rel": "dataflow", "urn": f"urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow={agency}:{flow}({version})"}
    return {"rel": "dataflow", "href": series.provenance.query_url or series.provenance.web_url or "about:blank"}


def sdmx_data_message(
    series_list: list[CanonicalSeries], views: list[list[Observation]], language: str = "kr"
) -> dict[str, Any]:
    """One SDMX-JSON 2.1 data message; series dimensions come from the canonical dimensions."""
    dim_ids: list[str] = []
    for s in series_list:
        for d in s.dimensions:
            if d not in dim_ids and d not in ("TIME_PERIOD", "STAT_CODE"):
                dim_ids.append(d)
    dim_values: list[list[dict[str, Any]]] = [[] for _ in dim_ids]
    dim_index: list[dict[str, int]] = [{} for _ in dim_ids]
    periods = sorted({o.period for obs in views for o in obs})
    period_index = {p: i for i, p in enumerate(periods)}
    units: list[str] = []
    levels = any(_has_levels(v) for v in views)

    series_out: dict[str, Any] = {}
    for s, obs in zip(series_list, views):
        key_parts = []
        for i, d in enumerate(dim_ids):
            code = s.dimensions.get(d, "_Z")
            if code not in dim_index[i]:
                dim_index[i][code] = len(dim_values[i])
                dim_values[i].append({"id": ecos_sdmx.sdmx_id(code), "name": code})
            key_parts.append(str(dim_index[i][code]))
        unit = s.unit or ""
        if unit not in units:
            units.append(unit)
        series_out[":".join(key_parts)] = {
            "attributes": [units.index(unit)],
            "observations": {
                str(period_index[o.period]): [o.value, *([o.source_value] if levels else [])] for o in obs
            },
        }

    measures = [{"id": "OBS_VALUE", "name": "Observation value"}]
    if levels:
        measures.append({"id": "SOURCE_VALUE", "name": "Level before transformation"})
    first = series_list[0] if series_list else None
    annotations = [{"type": "PROVENANCE", "title": s.provenance.citation(s.title)} for s in series_list]
    if first and first.provider == "ECOS":
        annotations.insert(0, {"type": "ECOS_MCP_MAPPING", "title": ecos_sdmx.MAPPING_NOTE})
    structure: dict[str, Any] = {
        "name": first.title if first else "empty",
        "links": [_dataflow_link(first)] if first else [{"rel": "self", "href": "about:blank"}],
        "dimensions": {
            "dataSet": [],
            "series": [
                {"id": d, "name": d, "keyPosition": i, "values": dim_values[i] or [{"id": "_Z", "name": "_Z"}]}
                for i, d in enumerate(dim_ids)
            ],
            "observation": [
                {
                    "id": "TIME_PERIOD",
                    "name": "Time period",
                    "keyPosition": len(dim_ids),
                    "roles": ["TIME_PERIOD"],
                    "values": [{"value": p} for p in periods] or [{"value": ""}],
                }
            ],
        },
        "measures": {"observation": measures},
        "attributes": {
            "dataSet": [],
            "series": [
                {
                    "id": "UNIT_MEASURE",
                    "name": "Unit of measure",
                    "relationship": {"dimensions": dim_ids} if dim_ids else {"dataflow": {}},
                    "values": [{"value": u} for u in units] or [None],
                }
            ],
            "observation": [],
        },
        "annotations": annotations,
        "dataSets": [0],
    }
    return {
        "$schema": ecos_sdmx.DATA_SCHEMA,
        "meta": {
            "id": f"GESM-{uuid.uuid4().hex[:16]}",
            "test": False,
            "prepared": datetime.now(KST).isoformat(timespec="seconds"),
            "sender": {"id": "GESM", "name": "Global Economic Statistical MCP"},
            "contentLanguages": ["en" if language == "en" else "ko"],
        },
        "data": {
            "structures": [structure],
            "dataSets": [
                {
                    "structure": 0,
                    "action": "Information",
                    "annotations": list(range(len(annotations))),
                    "series": series_out,
                }
            ],
        },
    }
