"""Rebuild the search catalogs shipped with the package.

- catalog/data/dataflows.json : OECD, IMF and BIS dataflow ids and names (from the live APIs)
- catalog/data/ecos_items.json: item codes of frequently used ECOS tables (StatisticItemList)

Every code in these files comes from the provider APIs — nothing is typed by hand.

Usage:
    uv run python scripts/build_catalogs.py            # both
    uv run python scripts/build_catalogs.py dataflows  # one of them
ECOS_API_KEY speeds up the ECOS part (the sample key returns 10 rows per call).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

from global_economic_statistical_mcp.config import today_kst
from global_economic_statistical_mcp.ecos_client import EcosClient

DATA = Path(__file__).resolve().parent.parent / "src" / "global_economic_statistical_mcp" / "catalog" / "data"

DATAFLOW_LISTS = {
    "OECD": ("https://sdmx.oecd.org/public/rest/dataflow/all/all/latest", "application/vnd.sdmx.structure+json;version=1.0"),
    "IMF": ("https://api.imf.org/external/sdmx/3.0/structure/dataflow/*/*/+", "application/vnd.sdmx.structure+json;version=2.0.0"),
    "BIS": ("https://stats.bis.org/api/v2/structure/dataflow/BIS/*/+", "application/vnd.sdmx.structure+json;version=2.0.0"),
}

# ECOS tables whose items are worth searching by name (prices, rates, FX, housing, GDP).
ECOS_ITEM_TABLES = ["901Y009", "404Y014", "817Y002", "721Y001", "731Y001", "731Y004", "901Y062", "901Y063", "722Y001", "200Y102"]


def _text(value):
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), None)
    return value


async def build_dataflows() -> None:
    out = {}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
        for provider, (url, accept) in DATAFLOW_LISTS.items():
            response = await http.get(url, headers={"Accept": accept})
            response.raise_for_status()
            flows = response.json()["data"]["dataflows"]
            rows = []
            for f in flows:
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
    which = set(sys.argv[1:]) or {"dataflows", "items"}
    if "dataflows" in which:
        await build_dataflows()
    if "items" in which:
        await build_ecos_items()


if __name__ == "__main__":
    asyncio.run(main())
