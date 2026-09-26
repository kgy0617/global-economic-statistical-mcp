# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). It is distributed from GitHub only; nothing is
published to PyPI.

## [0.5.0] — 2026-09-26

**Global provider expansion**: SDMX 2.1 (ECB), SDMX 3.0 with SDMX-CSV (Eurostat) and a non-SDMX API
(World Bank Data360) now feed the same Canonical Model, validation and provenance. Seven institutions
in total.

### Added

- **ECB** (SDMX 2.1, SDMX-JSON 1.0; structures in SDMX-ML): deposit facility rate, 3-month Euribor,
  long-term convergence rates, HICP and inflation, and EUR/USD. ECB publishes US dollars per euro,
  so an `invert` mapping option turns it into euros per dollar and records the inversion in
  provenance.
- **Eurostat** (SDMX 3.0, SDMX-CSV 2.0; structures in SDMX-ML): HICP, unemployment (SA and NSA),
  GDP growth and levels, and house prices for the euro area and EU member states.
- **World Bank** through the Data360 API: a new provider adapter rather than a chained MCP server,
  plus live indicator search and indicator metadata. New annual concepts cover real GDP growth,
  CPI inflation (annual average), population, GDP per capita (PPP) and current account as % of
  GDP. Annual mappings were added to GDP levels, current account, reserves and exchange rates.
- SDMX-CSV 1.0/2.0 and SDMX-ML 2.1/3.0 structure parsers, and an HTTP layer that returns text and
  sends read-only POSTs.
- Provider aliases: `ESTAT`, `World Bank`, `WorldBank` and `Data360`.
- A declared reference area for dataflows without an area dimension (ECB exchange rates). The
  country check reports `info`, and provenance says the area comes from the catalog.
- Documented known difference: BIS's euro-area policy rate followed the MRO rate until 2024-09-17
  and the deposit facility rate since.
- Search catalogs now list 9,387 dataflows (ECB 104, Eurostat 7,603) and 170 World Bank databases.

### Changed

- Cross-validation compares sources at their most common frequency, and the reference separately
  with each source at another frequency. Only complete periods are aggregated, and chain-linked
  real GDP levels are rebased to a common period (`compare_rebased`).
- The daily cross-validation report covers 62 multi-institution concept–economy pairs over three
  years.
- Live tests verify 250 series across the six default economies. World Bank mappings are batched
  like OECD's.
- IMF requests time out after 120 seconds instead of 60. Balance-of-payments queries have taken
  close to a minute.

### Fixed

- An institution with no comparable periods was reported as `MATCH`; it is now `NOT_COMPARED`.
- The comparison frequency was chosen nondeterministically when two frequencies tied.

### Found while adding the institutions

- Euro-area HICP moved to new ECOICOP ver.2 dataflows in 2026 (ECB `HICP`, Eurostat
  `PRC_HICP_MINR`, 2025=100). The old ones, and OECD's EA20 CPI, stop at 2025-12.
- The euro area is EA21 from 2026-01. Codes now differ by institution and dataflow: BIS `XM`,
  IMF `G163`, OECD `EA20`/`EA`, ECB `U2`, Eurostat `EA21`/`EA`, World Bank `EMU`.

## [0.4.0] — 2026-09-26

First public release as **Global Economic Statistical MCP**: global economic statistics
infrastructure for AI-powered macro research. Earlier versions were released as `ecos-mcp`,
a Bank of Korea (ECOS) server; 0.4.0 renames the package and rebuilds it around a
country-agnostic concept layer.

### Added

- **Institutions**: OECD, IMF and BIS through one SDMX REST adapter (SDMX 2.1 and 3.0, SDMX-JSON 1.0
  and 2.0), alongside the Bank of Korea's ECOS REST API. On a 429, a per-provider circuit breaker
  pauses that provider and the resolver falls back to the next source.
- **Concept Catalog**: 26 concepts (policy and market rates, CPI and inflation, GDP growth and
  levels, unemployment, exchange rates, current account, goods balance, FX reserves, share prices,
  consumer and business confidence, house prices, PPI and Korea-specific series). Every mapping
  was checked against the live APIs, and the catalog records economies a provider does not
  publish.
- **Default economies** re-verified against the live APIs: KR, US, JP, CN, EA and GB (178 series).
  Area codes are translated per provider and dataflow; for the euro area these are BIS `XM`,
  IMF `G163`, OECD `EA20` and OECD `EA`.
- **Canonical Model**: one time-series model for every provider (period, frequency, unit, scale,
  base period, seasonal adjustment).
- **Validation layer**: eight checks on every series (country, frequency, unit, scale, period,
  missing, duplicate, revision). Cross-validation across institutions ends in `MATCH`, `DIFFER`
  (verifiable cause) or `UNRESOLVED` (unexplained difference, reported and never hidden), with the
  differences and an investigation status per institution. A registry of documented known
  differences records causes, each with its evidence.
- **Concept aggregation rules**: when frequencies are aligned, flows are summed, stocks take the
  end-of-period value, and rates and indices are averaged.
- **Validation ledger** with a daily-partitioned history, the latest state per institution pair and
  series, and retention (`GESM_LEDGER_RETENTION_DAYS`, default 90).
- **Daily cross-validation report** (`scripts/validation_report.py`) over all 31 multi-institution
  concept–economy pairs. CI runs it daily with the live suite and keeps the summary as an artifact.
- **Provenance** on every series: agency, dataset, series key, retrieval time, query URL (API key
  masked), transformations and a citation. `output_format="sdmx"` returns SDMX-JSON validated
  against the official schema.
- Search catalogs generated from the APIs: 1,680 OECD/IMF/BIS dataflows and 1,349 ECOS items.

### Changed

- Package, command and repository renamed to `global-economic-statistical-mcp`.
- `country` is required for concept queries. Only direct ECOS table queries default to KR.
- Tool descriptions, error messages and field names are in English. Korean remains as data
  (`name_ko`, ECOS labels, Korean search terms).
- `explain_indicator` without a country lists the sources of every default economy.

### Removed

- `convert_currency`, which was unused. Currency normalisation will return as a designed feature
  (spot vs period average vs period end, PPP).
- The PyPI publishing workflow.

### Fixed

- Monthly flows (e.g. the current account) were averaged instead of summed when compared with
  quarterly data, which understated them threefold.
- A provider's `NaN` entered the canonical series as a number instead of a missing value.
- Earlier catalog codes that returned no data, or data for a different series, were replaced with
  codes generated from the provider APIs.

### Known discrepancies

The 2026-09-26 cross-validation report found MATCH 25 · DIFFER 2 · UNRESOLVED 3 · NOT_COMPARED 1.
The unresolved cases are UK and Japan CPI inflation (IMF vs BIS) and the daily KRW/USD rate
(ECOS vs BIS); see the README.

## [0.3.0] — 2026-09-26 (`ecos-mcp`)

- SDMX concept layer for ECOS, item-level search, evidence citations and data harmonisation.

## [0.2.0] — 2026-09-26 (`ecos-mcp`)

- ECOS MCP server upgrade.

## [0.1.0] — 2026-09-26 (`ecos-mcp`)

- ECOS (Bank of Korea) Open API MCP server.
