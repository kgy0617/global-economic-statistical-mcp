"""Rebuild the search catalogs shipped with the package.

- catalog/data/dataflows.json : OECD, IMF, BIS, ECB and Eurostat dataflows and World Bank
                                Data360 databases, ids and names (from the live APIs)
- catalog/data/ecos_items.json: item codes of frequently used ECOS tables (StatisticItemList)

Every code in these files comes from the provider APIs — nothing is typed by hand.

Usage:
    uv run python scripts/build_catalogs.py                      # both
    uv run python scripts/build_catalogs.py dataflows            # one of them
    uv run python scripts/build_catalogs.py dataflows ECB WB     # only these providers (others kept)
ECOS_API_KEY speeds up the ECOS part (the sample key returns 10 rows per call).
"""

from __future__ import annotations

import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from global_economic_statistical_mcp.config import today_kst
from global_economic_statistical_mcp.ecos_client import EcosClient

DATA = Path(__file__).resolve().parent.parent / "src" / "global_economic_statistical_mcp" / "catalog" / "data"

DATAFLOW_LISTS = {
    "OECD": ("https://sdmx.oecd.org/public/rest/dataflow/all/all/latest", "application/vnd.sdmx.structure+json;version=1.0"),
    "IMF": ("https://api.imf.org/external/sdmx/3.0/structure/dataflow/*/*/+", "application/vnd.sdmx.structure+json;version=2.0.0"),
    "BIS": ("https://stats.bis.org/api/v2/structure/dataflow/BIS/*/+", "application/vnd.sdmx.structure+json;version=2.0.0"),
    "ECB": ("https://data-api.ecb.europa.eu/service/dataflow/ECB", "application/vnd.sdmx.structure+xml;version=2.1"),
    "EUROSTAT": (
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/dataflow/ESTAT/all/latest",
        "application/vnd.sdmx.structure+xml;version=2.1",
    ),
}
DATA360_SEARCH = "https://data360api.worldbank.org/data360/searchv2"

# ECOS tables whose items are worth searching by name (prices, rates, FX, housing, GDP).
ECOS_ITEM_TABLES = ["901Y009", "404Y014", "817Y002", "721Y001", "731Y001", "731Y004", "901Y062", "901Y063", "722Y001", "200Y102"]


def _text(value):
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), None)
    return value


def _xml_flows(content: bytes) -> list[dict]:
    rows = []
    for f in ET.fromstring(content).iter():
        if not f.tag.endswith("}Dataflow") or "$" in (f.get("id") or ""):
            continue  # Eurostat "$DV_" entries are derived views of other dataflows
        names = [n for n in f if n.tag.endswith("}Name")]
        name = next((n.text for n in names if n.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"), None)
        rows.append({"ref": f"{f.get('agencyID')}:{f.get('id')}({f.get('version')})", "name": name or (names[0].text if names else f.get("id"))})
    return rows


async def _data360_databases(http: httpx.AsyncClient) -> list[dict]:
    body = {
        "search": "*",
        "top": 1000,
        "filter": "type eq 'dataset' and (is_active ne false or is_active eq null)",
        "select": "series_description/database_id, series_description/name",
    }
    response = await http.post(DATA360_SEARCH, json=body)
    response.raise_for_status()
    rows = {}
    for item in response.json().get("value") or []:
        meta = item.get("series_description") or {}
        if meta.get("database_id"):
            rows[meta["database_id"]] = {"ref": meta["database_id"], "name": meta.get("name") or meta["database_id"]}
    return list(rows.values())


async def build_dataflows(only: set[str] | None = None) -> None:
    path = DATA / "dataflows.json"
    out = json.loads(path.read_text(encoding="utf-8"))["providers"] if only and path.exists() else {}
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as http:
        for provider, (url, accept) in DATAFLOW_LISTS.items():
            if only and provider not in only:
                continue
            response = await http.get(url, headers={"Accept": accept})
            response.raise_for_status()
            if "xml" in accept:
                rows = _xml_flows(response.content)
            else:
                rows = []
                for f in response.json()["data"]["dataflows"]:
                    if provider == "IMF" and "VINTAGE" in f["id"]:
                        continue  # frozen past editions of other dataflows
                    rows.append(
                        {
                            "ref": f"{f['agencyID']}:{f['id']}({f['version']})",
                            "name": _text(f.get("names") or f.get("name")) or f["id"],
                        }
                    )
            out[provider] = sorted(rows, key=lambda r: r["ref"])
            print(f"{provider}: {len(rows)} dataflows", file=sys.stderr)
        if not only or "WB" in only:
            out["WB"] = sorted(await _data360_databases(http), key=lambda r: r["ref"])
            print(f"WB: {len(out['WB'])} Data360 databases", file=sys.stderr)
    payload = {"generated_at": today_kst().isoformat(), "providers": out}
    (DATA / "dataflows.json").write_text(json.dumps(payload, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


async def build_ecos_items() -> None:
    client = EcosClient()
    page = 10 if client.is_sample_key else 1000
    sem = asyncio.Semaphore(4)
    tables = []
    try:
        for stat_code in ECOS_ITEM_TABLES:
            first = await client._request("StatisticItemList", "kr", 1, page, stat_code)
            total = first["total_count"]

            async def fetch(start: int, total: int = total, stat_code: str = stat_code) -> list[dict]:
                async with sem:
                    res = await client._request("StatisticItemList", "kr", start, min(start + page - 1, total), stat_code)
                    return res["rows"]

            rows = list(first["rows"])
            for chunk in await asyncio.gather(*(fetch(s) for s in range(page + 1, total + 1, page))):
                rows.extend(chunk)
            items: dict[tuple[str, str], dict] = {}
            for r in rows:
                key = (r.get("GRP_CODE") or "Group1", r["ITEM_CODE"])
                item = items.setdefault(
                    key,
                    {
                        "group": key[0],
                        "code": r["ITEM_CODE"],
                        "name": r["ITEM_NAME"],
                        "parent": r.get("P_ITEM_CODE"),
                        "unit": r.get("UNIT_NAME"),
                        "cycles": [],
                    },
                )
                if r.get("CYCLE") and r["CYCLE"] not in item["cycles"]:
                    item["cycles"].append(r["CYCLE"])
            name = rows[0]["STAT_NAME"] if rows else stat_code
            tables.append({"stat_code": stat_code, "stat_name": name, "items": list(items.values())})
            print(f"ECOS {stat_code}: {len(items)} items ({len(rows)}/{total} rows)", file=sys.stderr)
    finally:
        await client.close()
    payload = {"generated_at": today_kst().isoformat(), "source": "ECOS StatisticItemList", "tables": tables}
    (DATA / "ecos_items.json").write_text(json.dumps(payload, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")


async def main() -> None:
    args = sys.argv[1:]
    which = {a for a in args if a in ("dataflows", "items")} or {"dataflows", "items"}
    providers = {a.upper() for a in args if a not in ("dataflows", "items")}
    if "dataflows" in which:
        await build_dataflows(providers or None)
    if "items" in which:
        await build_ecos_items()


if __name__ == "__main__":
    asyncio.run(main())
