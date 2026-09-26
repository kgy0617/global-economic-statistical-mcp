"""Daily cross-validation report for the default economies.

Runs cross-validation for every concept that at least two institutions publish for each of
KR, US, JP, CN, EA and GB over the last three years (so annual sources have complete years to
compare with), and writes a small summary:

    <out>/summary.json   one entry per concept and economy: MATCH / DIFFER / UNRESOLVED / NOT_COMPARED,
                         differences and the investigation status per institution
    <out>/summary.md     the same as a table (used as the CI job summary)

Raw per-period records go to the validation ledger ($GESM_DATA_DIR) as usual; only the summary
is meant to be kept as a CI artifact, never committed.

Usage:
    uv run python scripts/validation_report.py [out_dir]   # default: validation-report
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from global_economic_statistical_mcp.catalog.concepts import CONCEPTS
from global_economic_statistical_mcp.catalog.countries import (
    DEFAULT_COUNTRIES,
    get_country,
)
from global_economic_statistical_mcp.config import get_default_date_range
from global_economic_statistical_mcp.model import ecos_to_canonical, now_kst_iso
from global_economic_statistical_mcp.service import StatService

STATUSES = ("MATCH", "DIFFER", "UNRESOLVED", "NOT_COMPARED")


def window(src):
    start, end = get_default_date_range(src.freq, recent_years=3)
    return ecos_to_canonical(start, src.freq), ecos_to_canonical(end, src.freq)


def pairs():
    for concept in CONCEPTS:
        for code in DEFAULT_COUNTRIES:
            country = get_country(code)
            if len({m.provider for m in concept.sources_for(country)}) >= 2:
                yield concept, country


async def check(service: StatService, concept, country) -> dict:
    result = await service.cross_check(concept, country, window)
    for _ in range(2):  # wait out rate limits rather than report them as data problems
        limited = [e for e in result.get("errors", []) if "RATE_LIMITED" in e.get("error", "")]
        if not limited:
            break
        await asyncio.sleep(service.http.cooldown_remaining(limited[0]["provider"]) + 1)
        service.http._cooldown_until.clear()
        result = await service.cross_check(concept, country, window)
    return {
        "concept_id": concept.id,
        "country": country.iso2,
        "validation_status": result.get("validation_status", "NOT_COMPARED"),
        "reference": result.get("reference"),
        "periods": result.get("counts", {}).get("periods"),
        "method": result.get("method"),
        "providers": {
            p: {k: a[k] for k in ("validation_status", "difference", "investigation")}
            for p, a in (result.get("agreement") or {}).items()
        },
        **({"reason": result["reason"]} if result.get("reason") else {}),
        **({"errors": [f"{e['provider']}: {e['error']}" for e in result["errors"]]} if result.get("errors") else {}),
    }


def markdown(report: dict) -> str:
    counts = report["counts"]
    lines = [
        f"## Cross-validation report — {report['generated_at']}",
        "",
        " · ".join(f"**{s}** {counts.get(s, 0)}" for s in STATUSES),
        "",
        "| Concept | Economy | Status | Institutions (vs reference) | Max abs. difference | Investigation |",
        "|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        providers = r["providers"] or {}
        failed = [e.split(":")[0] + " not retrieved" for e in r.get("errors", [])]
        vs = ", ".join([f"{p} {a['validation_status']}" for p, a in providers.items()] + failed) or r.get("reason") or ""
        diff = ", ".join(f"{p} {a['difference']['absolute_max']}" for p, a in providers.items())
        inv = ", ".join(f"{p}: {a['investigation']['status']}" for p, a in providers.items() if a["investigation"]["status"] != "not_needed")
        lines.append(f"| {r['concept_id']} | {r['country']} | {r['validation_status']} | {r['reference'] or ''} → {vs} | {diff} | {inv} |")
    return "\n".join(lines) + "\n"


async def main(out_dir: Path) -> int:
    service = StatService()
    results = []
    try:
        for concept, country in pairs():
            try:
                results.append(await check(service, concept, country))
            except Exception as e:  # noqa: BLE001 - one failure must not stop the report
                results.append({"concept_id": concept.id, "country": country.iso2, "validation_status": "NOT_COMPARED",
                                "reference": None, "providers": {}, "errors": [str(e)]})
            print(f"{results[-1]['concept_id']:24} {results[-1]['country']}  {results[-1]['validation_status']}", flush=True)
    finally:
        await service.close()
    report = {
        "generated_at": now_kst_iso(),
        "economies": list(DEFAULT_COUNTRIES),
        "counts": {s: sum(1 for r in results if r["validation_status"] == s) for s in STATUSES},
        "results": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "summary.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "validation-report"))))
