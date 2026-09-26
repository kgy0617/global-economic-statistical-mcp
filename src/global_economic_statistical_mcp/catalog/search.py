"""Search over the generated catalogs: ECOS items and OECD/IMF/BIS dataflows.

Codes come only from ``catalog/data/*.json`` (built from the provider APIs by
``scripts/build_catalogs.py``). Synonyms below are keyed by the *item name* ECOS uses,
so a synonym can never point at a wrong code.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"

# ECOS item name → extra search words (Korean colloquialisms and English).
ITEM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "쌀": ("rice", "백미", "곡물"),
    "달걀": ("계란", "egg", "eggs"),
    "라면": ("ramen", "instant noodles"),
    "사과": ("apple",),
    "배추": ("cabbage", "김장"),
    "돼지고기": ("pork", "삼겹살"),
    "국산쇠고기": ("한우", "소고기", "beef"),
    "우유": ("milk",),
    "휘발유": ("gasoline", "petrol", "기름값"),
    "경유": ("diesel",),
    "전기료": ("전기요금", "전기세", "electricity"),
    "도시가스": ("가스요금", "난방비", "city gas"),
    "상수도료": ("수도요금", "water"),
    "CD(91일)": ("cd금리", "cd rate"),
    "국고채(3년)": ("ktb3y", "treasury 3y"),
    "국고채(10년)": ("ktb10y", "treasury 10y"),
    "원/위안(매매기준율)": ("위안화", "cny", "yuan"),
    "원/유로": ("유로화", "eur", "euro"),
    "원/일본엔(100엔)": ("엔화", "jpy", "yen"),
    "원/영국파운드": ("파운드", "gbp"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


@cache
def _load(name: str) -> dict[str, Any]:
    path = DATA / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def items_generated_at() -> str | None:
    return _load("ecos_items.json").get("generated_at")


def dataflows_generated_at() -> str | None:
    return _load("dataflows.json").get("generated_at")


def search_items(query: str, limit: int = 15, stat_code: str | None = None) -> list[dict[str, Any]]:
    """Find ECOS items (e.g. '쌀', '휘발유', '위안화') across frequently used tables."""
    q = _norm(query)
    if not q:
        return []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    order = 0
    for table in _load("ecos_items.json").get("tables", []):
        if stat_code and table["stat_code"] != stat_code:
            continue
        for item in table["items"]:
            order += 1
            name = _norm(item["name"])
            synonyms = [_norm(s) for s in ITEM_SYNONYMS.get(item["name"], ())]
            if q == name or q in synonyms:
                score = 0
            elif name.startswith(q):
                score = 1
            elif q in name or any(q in s for s in synonyms):
                score = 2
            else:
                continue
            cycles = item.get("cycles") or []
            cycle = next((c for c in ("M", "D", "Q", "A") if c in cycles), cycles[0] if cycles else None)
            group = int(re.sub(r"\D", "", item.get("group", "Group1")) or 1)
            scored.append(
                (
                    score,
                    order,
                    {
                        "stat_code": table["stat_code"],
                        "stat_name": table["stat_name"],
                        "item_code": item["code"],
                        "item_name": item["name"],
                        "group": item.get("group"),
                        "unit": item.get("unit"),
                        "cycles": cycles,
                        "get_data_example": {
                            "stat_code": table["stat_code"],
                            "cycle": cycle,
                            f"item_code{group}": item["code"],
                        },
                    },
                )
            )
    scored.sort(key=lambda x: (x[0], x[1]))
    return [r for _, _, r in scored[:limit]]


def search_dataflows(query: str, provider: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Find OECD/IMF/BIS dataflows by words in their id or English name (all words must match)."""
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    catalog = _load("dataflows.json").get("providers", {})
    matches = []
    for prov, flows in catalog.items():
        if provider and prov != provider.upper():
            continue
        for f in flows:
            text = f"{f['ref']} {f['name']}".lower()
            if all(w in text for w in words):
                matches.append({"provider": prov, "dataflow": f["ref"], "name": f["name"]})
    return {"total_matches": len(matches), "generated_at": dataflows_generated_at(), "data": matches[:limit]}


def dataflow_name(provider: str, ref: str) -> str | None:
    prefix = ref.split("(")[0]
    for f in _load("dataflows.json").get("providers", {}).get(provider, []):
        if f["ref"].split("(")[0] == prefix:
            return f["name"]
    return None
