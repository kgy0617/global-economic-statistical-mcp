"""Rebuild src/global_economic_statistical_mcp/catalog/data/tables.json from the live ECOS StatisticTableList API.

Usage:
    ECOS_API_KEY=<your key> uv run python scripts/update_tables.py

The sample key works too, but it is limited to 10 rows per call, so it takes ~90 calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from global_economic_statistical_mcp.config import today_kst
from global_economic_statistical_mcp.ecos_client import EcosClient

OUTPUT = Path(__file__).resolve().parent.parent / "src" / "global_economic_statistical_mcp" / "catalog" / "data" / "tables.json"
FIELDS = ("P_STAT_CODE", "STAT_CODE", "STAT_NAME", "CYCLE", "SRCH_YN", "ORG_NAME")


async def fetch_all_tables(client: EcosClient) -> list[dict]:
    page_size = 10 if client.is_sample_key else 1000
    rows: list[dict] = []
    start = 1
    while True:
        res = await client.list_statistic_tables(start_count=start, end_count=start + page_size - 1)
        rows.extend(res["rows"])
        print(f"  {len(rows)}/{res['total_count']}", file=sys.stderr)
        if not res["has_more"] or not res["rows"]:
            return rows
        start += len(res["rows"])


async def main() -> None:
    client = EcosClient()
    try:
        rows = await fetch_all_tables(client)
    finally:
        await client.close()

    tables = [{field: row.get(field) for field in FIELDS} for row in rows]
    payload = {
        "generated_at": today_kst().isoformat(),
        "source": "ECOS StatisticTableList",
        "tables": tables,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(tables)} tables to {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
