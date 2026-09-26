"""Map ECOS tables onto the SDMX information model and emit SDMX-JSON 2.1 messages.

The Bank of Korea does not publish an SDMX service for ECOS, so the structures here
are derived from ECOS metadata by this server (maintenance agency ``ECOS_MCP``):

    ECOS                                   SDMX
    ─────────────────────────────────────  ─────────────────────────────────────────
    statistic table (STAT_CODE)            Dataflow ``{STAT_CODE}`` + DSD ``DSD_{STAT_CODE}``
    cycle (A/S/Q/M/SM/D)                   dimension FREQ, codelist CL_FREQ
    item group GroupN (GRP_NAME)           dimension ITEM_CODE{N}, codelist CL_{STAT}_ITEM_CODE{N}
    item code / name / parent item         code id / name / parent
    TIME                                   TIME_PERIOD (2024, 2024-S1, 2024-Q1, 2024-01, 2024-01-15)
    DATA_VALUE                             measure OBS_VALUE (+ YOY_PCT / POP_PCT when transformed)
    UNIT_NAME                              series attribute UNIT_MEASURE

SDMX ids may only contain ``A-Za-z0-9_@$-``. ECOS item codes outside that set (e.g. ``*AA``)
are escaped as ``$`` + two hex digits per UTF-8 byte (``*AA`` → ``$2AAA``); ``$`` itself is
escaped as ``$24`` so the mapping is reversible (see ``ecos_code``).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from global_economic_statistical_mcp.catalog.concepts import CONCEPTS
from global_economic_statistical_mcp.catalog.countries import get_country
from global_economic_statistical_mcp.config import KST
from global_economic_statistical_mcp.timeseries import TRANSFORMS, series_key, to_number

AGENCY = "ECOS_MCP"
VERSION = "1.0"
DATA_SCHEMA = "https://json.sdmx.org/2.1/sdmx-json-data-schema.json"
STRUCTURE_SCHEMA = "https://json.sdmx.org/2.1/sdmx-json-structure-schema.json"

# Highest to lowest frequency; used for ordering and frequency conversion.
FREQ_ORDER = ["D", "SM", "M", "Q", "S", "A"]
FREQ_NAMES = {
    "kr": {"A": "연간", "S": "반기", "Q": "분기", "M": "월간", "SM": "반월", "D": "일간"},
    "en": {"A": "Annual", "S": "Half-yearly", "Q": "Quarterly", "M": "Monthly", "SM": "Semi-monthly", "D": "Daily"},
}
CONCEPT_NAMES = {
    "kr": {
        "FREQ": "주기",
        "TIME_PERIOD": "시점",
        "OBS_VALUE": "관측값",
        "UNIT_MEASURE": "단위",
        "YOY_PCT": "전년동기대비 증감률(%)",
        "POP_PCT": "직전 관측치 대비 증감률(%)",
    },
    "en": {
        "FREQ": "Frequency",
        "TIME_PERIOD": "Time period",
        "OBS_VALUE": "Observation value",
        "UNIT_MEASURE": "Unit of measure",
        "YOY_PCT": "Year-on-year change (%)",
        "POP_PCT": "Period-on-period change (%)",
    },
}
MAPPING_NOTE = (
    "Unofficial SDMX mapping of Bank of Korea ECOS metadata produced by global-economic-statistical-mcp; "
    "the Bank of Korea does not publish SDMX structures for ECOS."
)

_SAFE_ID = re.compile(r"[A-Za-z0-9_@-]")


# ── Identifiers and URNs ────────────────────────────────────────────


def sdmx_id(ecos_code: str) -> str:
    """Escape an ECOS code into a valid SDMX id (reversible)."""
    out = []
    for char in ecos_code:
        if _SAFE_ID.fullmatch(char):
            out.append(char)
        else:
            out.extend(f"${byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out) or "_"


def ecos_code(identifier: str) -> str:
    """Inverse of sdmx_id."""
    raw = bytearray()
    i = 0
    while i < len(identifier):
        if identifier[i] == "$":
            raw.append(int(identifier[i + 1 : i + 3], 16))
            i += 3
        else:
            raw.extend(identifier[i].encode("utf-8"))
            i += 1
    return raw.decode("utf-8")


def _urn(package: str, cls: str, artefact_id: str, item: str | None = None) -> str:
    base = f"urn:sdmx:org.sdmx.infomodel.{package}.{cls}={AGENCY}:{artefact_id}({VERSION})"
    return f"{base}.{item}" if item else base


def dataflow_urn(stat_code: str) -> str:
    return _urn("datastructure", "Dataflow", sdmx_id(stat_code))


def dsd_urn(stat_code: str) -> str:
    return _urn("datastructure", "DataStructure", f"DSD_{sdmx_id(stat_code)}")


def concept_urn(stat_code: str, concept: str) -> str:
    return _urn("conceptscheme", "Concept", f"CS_{sdmx_id(stat_code)}", concept)


def codelist_urn(codelist_id: str) -> str:
    return _urn("codelist", "Codelist", codelist_id)


def item_codelist_id(stat_code: str, dimension_id: str) -> str:
    return f"CL_{sdmx_id(stat_code)}_{dimension_id}"


def sdmx_time(cycle: str, value: str) -> str:
    """Convert an ECOS period string to SDMX reporting-period syntax."""
    value = str(value)
    if cycle == "S":
        return f"{value[:4]}-{value[4:]}"  # 2024S1 → 2024-S1
    if cycle == "Q":
        return f"{value[:4]}-{value[4:]}"  # 2024Q1 → 2024-Q1
    if cycle == "M":
        return f"{value[:4]}-{value[4:6]}"
    if cycle == "SM":  # no SDMX equivalent; kept recognisable
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if cycle == "D":
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value


def _meta(language: str) -> dict[str, Any]:
    return {
        "id": f"ECOS_MCP-{uuid.uuid4().hex[:16]}",
        "test": False,
        "prepared": datetime.now(KST).isoformat(timespec="seconds"),
        "sender": {"id": AGENCY, "name": "Global Economic Statistical MCP"},
        "contentLanguages": ["en" if language == "en" else "ko"],
    }


def _concept_name(concept: str, language: str) -> str:
    return CONCEPT_NAMES["en" if language == "en" else "kr"].get(concept, concept)


# ── Table structure model ───────────────────────────────────────────


@dataclass
class CodeInfo:
    code: str
    name: str
    parent: str | None = None
    unit: str | None = None
    availability: dict[str, str] = field(default_factory=dict)  # cycle -> "start~end"

    def compact(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "name": self.name}
        if self.parent:
            out["parent"] = self.parent
        if self.unit:
            out["unit"] = self.unit
        if self.availability:
            out["availability"] = self.availability
        return out


@dataclass
class DimensionInfo:
    position: int  # 1..4, matching ITEM_CODE{n}
    concept_name: str
    codes: list[CodeInfo]
    total_codes: int

    @property
    def id(self) -> str:
        return f"ITEM_CODE{self.position}"


@dataclass
class TableStructure:
    stat_code: str
    stat_name: str
    cycles: list[str]
    dimensions: list[DimensionInfo]
    complete: bool = True  # all item rows were retrieved
    partial: bool = False  # codes were filtered or truncated

    @property
    def series_key(self) -> list[str]:
        return ["FREQ", *(d.id for d in self.dimensions)]


def linked_concepts(stat_code: str) -> list[dict[str, Any]]:
    """Canonical concepts that use this ECOS table, with their other (international) sources for Korea."""
    korea = get_country("KR")
    out = []
    for c in CONCEPTS:
        ecos = [m for m in c.sources if m.provider == "ECOS" and m.dataflow == stat_code]
        if not ecos:
            continue
        out.append(
            {
                "concept_id": c.id,
                "name": c.name_ko,
                "ecos_keys": [{"cycle": m.freq, "key": m.key} for m in ecos],
                "other_sources_for_KR": [
                    {"provider": m.provider, "dataflow": m.dataflow, "key": m.render_key(korea), "freq": m.freq}
                    for m in c.sources
                    if m.provider != "ECOS"
                ],
            }
        )
    return out


def build_table_structure(
    stat_code: str,
    stat_name: str,
    item_rows: list[dict[str, Any]],
    complete: bool = True,
) -> TableStructure:
    """Build a table structure from StatisticItemList rows.

    ECOS lists one row per (group, item, cycle); rows are merged per (group, item).
    """
    groups: dict[int, dict[str, Any]] = {}
    cycles: set[str] = set()
    for row in item_rows:
        match = re.search(r"(\d+)$", str(row.get("GRP_CODE") or "Group1"))
        position = int(match.group(1)) if match else 1
        group = groups.setdefault(position, {"name": row.get("GRP_NAME") or f"항목{position}", "codes": {}})
        code = str(row.get("ITEM_CODE") or "")
        if not code:
            continue
        info = group["codes"].get(code)
        if info is None:
            info = CodeInfo(
                code=code,
                name=row.get("ITEM_NAME") or code,
                parent=row.get("P_ITEM_CODE") or None,
                unit=row.get("UNIT_NAME") or None,
            )
            group["codes"][code] = info
        cycle = row.get("CYCLE")
        if cycle:
            cycles.add(cycle)
            info.availability[cycle] = f"{row.get('START_TIME', '')}~{row.get('END_TIME', '')}"

    dimensions = [
        DimensionInfo(
            position=position,
            concept_name=group["name"],
            codes=list(group["codes"].values()),
            total_codes=len(group["codes"]),
        )
        for position, group in sorted(groups.items())
    ]
    ordered_cycles = [c for c in FREQ_ORDER if c in cycles]
    return TableStructure(stat_code, stat_name, ordered_cycles, dimensions, complete=complete)


def filter_structure(
    structure: TableStructure,
    item_keyword: str | None = None,
    codes_limit: int | None = None,
) -> TableStructure:
    """Keep codes whose name or code contains item_keyword, up to codes_limit per dimension."""
    keyword = re.sub(r"\s+", "", item_keyword or "").lower()
    dimensions = []
    partial = structure.partial
    for dim in structure.dimensions:
        codes = dim.codes
        if keyword:
            codes = [
                c for c in codes
                if keyword in re.sub(r"\s+", "", c.name).lower() or keyword in c.code.lower()
            ]
        if codes_limit is not None and len(codes) > codes_limit:
            codes = codes[:codes_limit]
        if len(codes) != len(dim.codes):
            partial = True
        dimensions.append(DimensionInfo(dim.position, dim.concept_name, codes, dim.total_codes))
    return TableStructure(
        structure.stat_code,
        structure.stat_name,
        structure.cycles,
        dimensions,
        complete=structure.complete,
        partial=partial,
    )


def compact_structure(structure: TableStructure, language: str = "kr") -> dict[str, Any]:
    """Token-light view of the table structure for LLMs."""
    freq_names = FREQ_NAMES["en" if language == "en" else "kr"]
    example = {
        "stat_code": structure.stat_code,
        "cycle": structure.cycles[0] if structure.cycles else None,
        **{
            f"item_code{d.position}": d.codes[0].code
            for d in structure.dimensions
            if d.codes
        },
    }
    out: dict[str, Any] = {
        "dataflow": {"id": structure.stat_code, "name": structure.stat_name, "urn": dataflow_urn(structure.stat_code)},
        "series_key": structure.series_key,
        "dimensions": [
            {
                "id": "FREQ",
                "name": _concept_name("FREQ", language),
                "codes": [{"code": c, "name": freq_names[c]} for c in structure.cycles],
            },
            *(
                {
                    "id": d.id,
                    "name": d.concept_name,
                    "total_codes": d.total_codes,
                    "codes": [c.compact() for c in d.codes],
                }
                for d in structure.dimensions
            ),
        ],
        "time_dimension": "TIME_PERIOD",
        "measures": ["OBS_VALUE"],
        "attributes": ["UNIT_MEASURE"],
        "get_data_example": {k: v for k, v in example.items() if v is not None},
    }
    if structure.partial:
        out["partial"] = True
    if not structure.complete:
        out["complete"] = False
        out["note"] = "API 한도로 항목 목록 일부만 조회했습니다 (sample 키 등). 정식 키를 쓰면 전체 항목이 조회됩니다."
    linked = linked_concepts(structure.stat_code)
    if linked:
        out["canonical_concepts"] = linked
    return out


def structure_message(structure: TableStructure, language: str = "kr") -> dict[str, Any]:
    """SDMX-JSON 2.1 structure message: dataflow, DSD, codelists and concept scheme."""
    stat = sdmx_id(structure.stat_code)
    freq_names = FREQ_NAMES["en" if language == "en" else "kr"]
    lang_note = [{"type": "ECOS_MCP_MAPPING", "title": MAPPING_NOTE}]
    for c in linked_concepts(structure.stat_code):
        lang_note.append({"type": "CANONICAL_CONCEPT", "title": c["concept_id"]})
        for other in c["other_sources_for_KR"]:
            lang_note.append({"type": "CROSS_AGENCY", "title": f"{other['provider']} {other['dataflow']} {other['key']}"})
    def code_entry(c: CodeInfo, known: set[str]) -> dict[str, Any]:
        entry: dict[str, Any] = {"id": sdmx_id(c.code), "name": c.name}
        if c.parent and c.parent in known:
            entry["parent"] = sdmx_id(c.parent)
        annotations = []
        if sdmx_id(c.code) != c.code:
            annotations.append({"type": "ECOS_ITEM_CODE", "title": c.code})
        if c.unit:
            annotations.append({"type": "ECOS_UNIT", "title": c.unit})
        if c.availability:
            annotations.append(
                {
                    "type": "ECOS_AVAILABILITY",
                    "title": ", ".join(f"{k}:{v}" for k, v in c.availability.items()),
                }
            )
        if annotations:
            entry["annotations"] = annotations
        return entry

    codelists: list[dict[str, Any]] = [
        {
            "id": "CL_FREQ",
            "agencyID": AGENCY,
            "version": VERSION,
            "name": _concept_name("FREQ", language),
            "codes": [{"id": c, "name": freq_names[c]} for c in FREQ_ORDER],
        }
    ]
    for d in structure.dimensions:
        known = {c.code for c in d.codes}
        codelist: dict[str, Any] = {
            "id": item_codelist_id(structure.stat_code, d.id),
            "agencyID": AGENCY,
            "version": VERSION,
            "name": d.concept_name,
            "codes": [code_entry(c, known) for c in d.codes],
        }
        if structure.partial or not structure.complete:
            codelist["isPartial"] = True
        codelists.append(codelist)

    dimensions = [
        {
            "id": "FREQ",
            "position": 0,
            "conceptIdentity": concept_urn(structure.stat_code, "FREQ"),
            "localRepresentation": {"enumeration": codelist_urn("CL_FREQ")},
        },
        *(
            {
                "id": d.id,
                "position": i,
                "conceptIdentity": concept_urn(structure.stat_code, d.id),
                "localRepresentation": {
                    "enumeration": codelist_urn(item_codelist_id(structure.stat_code, d.id))
                },
            }
            for i, d in enumerate(structure.dimensions, start=1)
        ),
    ]
    concepts = [
        {"id": "FREQ", "name": _concept_name("FREQ", language)},
        *({"id": d.id, "name": d.concept_name} for d in structure.dimensions),
        *(
            {"id": c, "name": _concept_name(c, language)}
            for c in ("TIME_PERIOD", "OBS_VALUE", "UNIT_MEASURE")
        ),
    ]
    return {
        "$schema": STRUCTURE_SCHEMA,
        "meta": _meta(language),
        "data": {
            "dataflows": [
                {
                    "id": stat,
                    "agencyID": AGENCY,
                    "version": VERSION,
                    "name": structure.stat_name,
                    "structure": dsd_urn(structure.stat_code),
                    "annotations": lang_note,
                }
            ],
            "dataStructures": [
                {
                    "id": f"DSD_{stat}",
                    "agencyID": AGENCY,
                    "version": VERSION,
                    "name": structure.stat_name,
                    "annotations": lang_note,
                    "dataStructureComponents": {
                        "dimensionList": {
                            "id": "DimensionDescriptor",
                            "dimensions": dimensions,
                            "timeDimension": {
                                "id": "TIME_PERIOD",
                                "conceptIdentity": concept_urn(structure.stat_code, "TIME_PERIOD"),
                                "localRepresentation": {"format": {"dataType": "ObservationalTimePeriod"}},
                            },
                        },
                        "attributeList": {
                            "id": "AttributeDescriptor",
                            "attributes": [
                                {
                                    "id": "UNIT_MEASURE",
                                    "usage": "optional",
                                    "attributeRelationship": {"dimensions": structure.series_key},
                                    "conceptIdentity": concept_urn(structure.stat_code, "UNIT_MEASURE"),
                                    "localRepresentation": {"format": {"dataType": "String"}},
                                }
                            ],
                        },
                        "measureList": {
                            "id": "MeasureDescriptor",
                            "measures": [
                                {
                                    "id": "OBS_VALUE",
                                    "conceptIdentity": concept_urn(structure.stat_code, "OBS_VALUE"),
                                    "localRepresentation": {"format": {"dataType": "Double"}},
                                }
                            ],
                        },
                    },
                }
            ],
            "codelists": codelists,
            "conceptSchemes": [
                {
                    "id": f"CS_{stat}",
                    "agencyID": AGENCY,
                    "version": VERSION,
                    "name": structure.stat_name,
                    "concepts": concepts,
                }
            ],
        },
    }


# ── Data message ────────────────────────────────────────────────────


def data_message(
    rows: list[dict[str, Any]],
    *,
    stat_code: str,
    cycle: str,
    transform: str | None = None,
    notes: list[str] | None = None,
    language: str = "kr",
) -> dict[str, Any]:
    """SDMX-JSON 2.1 data message for StatisticSearch rows (series-level organisation)."""
    freq_names = FREQ_NAMES["en" if language == "en" else "kr"]
    stat_name = rows[0].get("STAT_NAME", stat_code) if rows else stat_code
    item_depth = max(
        (len([c for c in series_key(r) if c]) for r in rows),
        default=0,
    )

    dim_values: list[list[dict[str, Any]]] = [[{"id": cycle, "name": freq_names.get(cycle, cycle)}]]
    dim_index: list[dict[str, int]] = [{cycle: 0}]
    for _ in range(item_depth):
        dim_values.append([])
        dim_index.append({})

    times = sorted({str(r.get("TIME", "")) for r in rows})
    time_index = {t: i for i, t in enumerate(times)}
    units: list[str] = []

    measure_fields = ["DATA_VALUE"] + ([TRANSFORMS[transform]] if transform else [])
    series: dict[str, dict[str, Any]] = {}
    for r in rows:
        key_parts = [0]
        for position in range(1, item_depth + 1):
            code = r.get(f"ITEM_CODE{position}") or ""
            lookup = dim_index[position]
            if code not in lookup:
                lookup[code] = len(dim_values[position])
                dim_values[position].append(
                    {"id": sdmx_id(code), "name": r.get(f"ITEM_NAME{position}") or code}
                )
            key_parts.append(lookup[code])
        key = ":".join(str(p) for p in key_parts)

        entry = series.get(key)
        if entry is None:
            unit = r.get("UNIT_NAME") or ""
            if unit not in units:
                units.append(unit)
            entry = {"attributes": [units.index(unit)], "observations": {}}
            series[key] = entry
        values = [to_number(r.get(f)) if f == "DATA_VALUE" else r.get(f) for f in measure_fields]
        entry["observations"][str(time_index[str(r.get("TIME", ""))])] = [
            v if isinstance(v, int | float) or v is None else str(v) for v in values
        ]

    series_dims = [
        {
            "id": "FREQ",
            "name": _concept_name("FREQ", language),
            "keyPosition": 0,
            "values": dim_values[0],
        },
        *(
            {
                "id": f"ITEM_CODE{position}",
                "name": f"ITEM_CODE{position}",
                "keyPosition": position,
                "values": dim_values[position],
            }
            for position in range(1, item_depth + 1)
            if dim_values[position]
        ),
    ]
    measures = [{"id": "OBS_VALUE", "name": _concept_name("OBS_VALUE", language)}]
    if transform:
        measure_id = TRANSFORMS[transform].upper()
        measures.append({"id": measure_id, "name": _concept_name(measure_id, language)})

    annotations = [{"type": "ECOS_MCP_MAPPING", "title": MAPPING_NOTE}]
    annotations.extend({"type": "NOTE", "title": n} for n in (notes or []) if n)

    structure: dict[str, Any] = {
        "name": stat_name,
        "links": [
            {"rel": "dataflow", "urn": dataflow_urn(stat_code)},
            {"rel": "datastructure", "urn": dsd_urn(stat_code)},
        ],
        "dimensions": {
            "dataSet": [],
            "series": series_dims,
            "observation": [
                {
                    "id": "TIME_PERIOD",
                    "name": _concept_name("TIME_PERIOD", language),
                    "keyPosition": len(series_dims),
                    "roles": ["TIME_PERIOD"],
                    "values": [{"value": sdmx_time(cycle, t)} for t in times] or [{"value": ""}],
                }
            ],
        },
        "measures": {"observation": measures},
        "attributes": {
            "dataSet": [],
            "series": [
                {
                    "id": "UNIT_MEASURE",
                    "name": _concept_name("UNIT_MEASURE", language),
                    "relationship": {"dimensions": [d["id"] for d in series_dims]},
                    "values": [{"value": u} for u in units] or [None],
                }
            ],
            "observation": [],
        },
        "annotations": annotations,
        "dataSets": [0],
    }
    return {
        "$schema": DATA_SCHEMA,
        "meta": _meta(language),
        "data": {
            "structures": [structure],
            "dataSets": [
                {
                    "structure": 0,
                    "action": "Information",
                    "annotations": list(range(len(annotations))),
                    "series": series,
                }
            ],
        },
    }
