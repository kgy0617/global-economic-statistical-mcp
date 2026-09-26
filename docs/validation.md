# Validation in detail

The README gives the short version. This page covers how comparisons are made, the latest
cross-validation report, the documented known differences, and where results are stored.

## Checks on every series

Every response carries these checks. Each result is `pass`, `info`, `warn` or `fail`.

| Check | What it verifies |
|---|---|
| `country` | The data is for the requested economy, which also catches providers that ignore a key filter. When a dataflow has no area dimension (ECB exchange rates), the result is `info` and the area comes from the catalog. |
| `frequency` | The frequency matches and every period can be parsed. |
| `unit` | The unit is compatible with the concept, and an index has the expected base period. |
| `scale` | The unit multiplier (10^n) is as expected. |
| `period` | Periods are in order and inside the requested window. |
| `missing` | Missing values (including a provider's `NaN`), gaps and periods not yet published. |
| `duplicate` | No period appears twice. |
| `revision` | Whether the provider revised values since the last retrieval (compared on raw values). |

## How values are aligned for cross-validation

- **Aggregation by concept**: flows (current account, goods balance, GDP) are summed, stocks
  (FX reserves) take the end-of-period value, and rates, prices and indices are averaged.
- **Complete periods only**: two quarters are not a year, so partial periods are never compared.
- **One comparison per frequency**: sources are compared at their most common frequency. A source
  published only at another frequency (for example annual World Bank data next to quarterly IMF
  data) is compared with the reference separately.
- **Rebasing**: indices with different base years are rebased to a common period. So are
  chain-linked real GDP levels, whose scale depends on each institution's reference year.

Tolerances for `MATCH`: 0.05pp for rates, 0.1% for indices (1% after rebasing), 0.5% otherwise.
`DIFFER` needs a verifiable cause: different seasonal adjustment, the precision a provider
publishes, or a documented known difference.

Each institution's result carries the difference and the investigation status:

```json
"OECD": {"vs": "IMF", "validation_status": "UNRESOLVED",
         "difference": {"absolute_max": 0.06751, "absolute_mean": 0.0225, "relative_max_pct": 2.2263},
         "investigation": {"status": "unresolved", "unresolved_periods": ["2026-01"]}}
```

## Latest report (2026-09-26)

Six default economies, last three years, 62 concept–economy pairs with two or more institutions:
**MATCH 43 · DIFFER 3 · UNRESOLVED 15 · NOT_COMPARED 1**.

| Concept · economy | Institutions | Status |
|---|---|---|
| Policy rate, 10-year and 3-month rates, unemployment (NSA and SA) · KR | ECOS vs BIS / OECD | MATCH, identical |
| Current account, goods balance · KR | ECOS (monthly, summed) vs IMF (quarterly) | MATCH, identical |
| Real GDP · all six economies | ECOS / IMF vs Eurostat / World Bank | MATCH after rebasing |
| Nominal GDP · KR, US, JP, CN, GB | ECOS / IMF vs World Bank (annual) | MATCH |
| CPI · KR, US, JP, CN, EA | ECOS / IMF / Eurostat vs ECB, BIS, OECD | MATCH after rebasing |
| CPI inflation · KR, US | ECOS / IMF vs BIS, OECD | MATCH |
| Euro-area HICP inflation | Eurostat vs ECB | MATCH, identical |
| Unemployment, GDP growth, house prices, 3-month rate · EA | OECD / BIS vs Eurostat / ECB | MATCH |
| Exchange rate per USD, monthly · KR, CN, EA, GB | ECOS / IMF vs BIS, OECD, ECB, World Bank | MATCH |
| Current account, FX reserves · JP, CN | IMF vs World Bank | MATCH |
| Euro-area policy rate | BIS vs ECB | **DIFFER**: known difference (see below) |
| Real GDP growth, year on year · KR | ECOS vs OECD | **DIFFER**: seasonal adjustment differs (ECOS NSA, OECD SA) |
| Consumer sentiment · KR | ECOS vs OECD | **DIFFER**: known difference (see below) |
| CPI inflation · GB, JP, CN, EA | IMF / Eurostat vs BIS (and OECD) | **UNRESOLVED**: BIS up to 0.96pp apart for GB, 0.20pp for JP; others about 0.05–0.1pp |
| CPI · GB | IMF vs BIS | **UNRESOLVED**: BIS index 1.4% apart after rebasing |
| Long-term rate · EA | OECD vs ECB | **UNRESOLVED**: up to 0.13pp (EA20 benchmark vs ECB convergence rate) |
| Nominal GDP · EA | IMF vs Eurostat | **UNRESOLVED**: 1.4% (euro-area composition or vintage, not yet confirmed) |
| Exchange rate per USD · JP | IMF vs BIS, OECD | **UNRESOLVED**: up to 0.9% in some months (World Bank matches IMF) |
| KRW per USD, daily | ECOS vs BIS | **UNRESOLVED**: up to 28 won (2%) in recent days; not a one-day lag |
| Current account · US, EA, GB; FX reserves · US, EA, GB | IMF vs World Bank | **UNRESOLVED**: World Bank annual figures differ by 1–280% (vintage; for reserves, gold valuation and coverage) |
| KOSPI · KR | ECOS vs OECD | NOT_COMPARED: with the ECOS sample key only the latest 10 days come back |

## Documented known differences

Each has its evidence in `KNOWN_DIFFERENCES` in `catalog/concepts.py`. Entries are added only with
evidence, never to silence a mismatch.

- **Euro-area policy rate, BIS vs ECB**: BIS followed the main refinancing rate until the ECB's
  operational framework change and the deposit facility rate since. BIS − DFR = 0.50 every day
  through 2024-09-17, identical from 2024-09-18.
- **Korean consumer sentiment, ECOS vs OECD**: the OECD indicator is amplitude-adjusted (key
  component `AA`); the Bank of Korea CCSI is not.

## Where results are stored

```
$GESM_DATA_DIR/validation/
├── current/latest.json          latest status per institution pair and per series
└── history/YYYY-MM-DD.jsonl     every record of that day; deleted after GESM_LEDGER_RETENTION_DAYS (default 90)
```

The `gesm://validation/summary` resource shows the agreement rate and the MATCH / DIFFER /
UNRESOLVED counts per concept, economy and institution pair, plus the latest status. Revision
snapshots are kept under `revisions/`.

## Daily report

`scripts/validation_report.py` cross-validates every multi-institution concept–economy pair (62)
over the last three years and writes `summary.json` and `summary.md`. CI runs it daily, shows the
table in the job summary and keeps it as a 90-day artifact; raw records are not uploaded.
