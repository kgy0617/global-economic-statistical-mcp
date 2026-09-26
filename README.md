# 🌏 Global Economic Statistical MCP

**Global economic statistics infrastructure for AI-powered macro research.**

Global Economic Statistical MCP is an AI-native interface for discovering, retrieving, comparing, and analyzing trusted macroeconomic statistics across central banks and international organizations.

It is built for macroeconomic research and analysis, not for trading signals. An LLM agent asks for a *concept*, such as the policy rate, CPI inflation, real GDP or the current account, for an *economy*. The server resolves it to the right official series at the Bank of Korea (ECOS), OECD, IMF or BIS, converts the result to one canonical time-series model, validates it and cross-checks it against other institutions. Every number it returns carries its provenance.

```
                          LLM
                           │
                           ▼
                  ┌─────────────────┐
                  │ Statistical MCP │
                  │    6 Tools      │
                  └────────┬────────┘
                           │
                           ▼
                  ┌─────────────────┐
                  │ Concept Resolver│
                  └────────┬────────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
      Concept Catalog             Provider Catalog
             │                           │
             └─────────────┬─────────────┘
                           ▼
                  Provider Resolver
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
           ECOS          SDMX          Future
             │        ┌────┼────┐
             │        ▼    ▼    ▼
             │       OECD IMF  BIS
             │        │    │    │
             └────────┴────┴────┘
                           │
                           ▼
                  ┌─────────────────┐
                  │ Canonical Model │
                  └────────┬────────┘
                           │
                    ┌──────┴──────┐
                    ▼             ▼
               Validation     Provenance
                    │             │
                    └──────┬──────┘
                           ▼
                       Analysis
```

Every adapter converts its response into the Canonical Model and attaches provenance at that moment. Validation checks the same canonical series, and analysis (`compare_series`, `calculate_statistics`) works on series that carry both.

The MCP tools are only the interface. What the project really delivers is four things behind them:

- **Concept Catalog**: country-agnostic economic concepts, each mapped to the series every institution publishes for it.
- **Canonical Model**: one time-series model (period, frequency, unit, scale, base period, adjustment) for data from every provider.
- **Validation**: checks on every series, cross-validation between institutions, and a ledger of the results.
- **Provenance**: the agency, dataset, series key, retrieval time, query URL, transformations and a citation for every series.

---

## 🌐 Coverage

### Default economies (verified)

Every Concept Catalog mapping is re-verified against the live APIs for these six economies by `uv run pytest -m live`, and daily in CI.

| Code | Economy | Area codes the server translates to |
|---|---|---|
| `KR` | Korea | ECOS (national source) · `KOR` / `KR` |
| `US` | United States | `USA` / `US` |
| `JP` | Japan | `JPN` / `JP` |
| `CN` | China | `CHN` / `CN` |
| `EA` | Euro area | BIS `XM` · IMF `G163` · OECD `EA20` (national accounts and labour: `EA`) |
| `GB` | United Kingdom | `GBR` / `GB` |

About 40 more economies (G20 and OECD members, major emerging markets) resolve through the same key templates. They are not re-verified: each response's validation report shows whether the data came back as expected. The full list is in the `gesm://countries` resource.

When an institution does not publish a concept for an economy, the catalog records that. For example, OECD has no CPI for Japan and no unemployment rate for China, and a US-dollar exchange rate means nothing for the United States. In those cases the resolver skips that source or names the sources that are available, instead of returning an empty series.

### Institutions

Korea's central bank has no SDMX endpoint, so ECOS is connected through its REST API. The other institutions are connected through SDMX. Only ECOS needs a key.

| Institution | Endpoint | Protocol | Notes from the live APIs |
|---|---|---|---|
| **ECOS** (Bank of Korea) | `ecos.bok.or.kr/api` | ECOS REST | Needs `ECOS_API_KEY` (without one, the sample key returns 10 rows per call). The server maps ECOS table structures to SDMX concepts. |
| **OECD** | `sdmx.oecd.org/public/rest` | SDMX 2.1 · JSON 2.0 | **Strict per-IP rate limit (HTTP 429).** The server sends one request at a time. On a 429 it puts OECD on cooldown and falls back to the next source. |
| **IMF** | `api.imf.org/external/sdmx/3.0` | SDMX 3.0 · JSON 2.0 | The old API (dataservices.imf.org) is retired. **A key containing `*` makes the API ignore even the country filter**, so results are filtered again on arrival. CPI base years differ by country (KR 2020, US 2010). |
| **BIS** | `stats.bis.org/api/v1` (data) · `/v2` (structure) | SDMX 2.1 · 3.0 | Policy and exchange rates carry no unit attribute. For those series the unit declared in the catalog is used, and the validation report marks it as `info`. |

---

## 🧰 MCP tools

| Tool | What it does |
|---|---|
| `search_statistics` | Searches concepts, OECD/IMF/BIS dataflows, ECOS tables and items, and the Bank of Korea's 100 key statistics. |
| `get_metadata` | Returns the real DSD and codelists of an OECD, IMF or BIS dataflow, or an ECOS table's structure mapped to SDMX, with its linked concepts and international sources. |
| `get_data` | Retrieves a concept for an economy (`indicator="CPI_YOY", country="EA"`), an ECOS table or an SDMX dataflow, with validation and provenance. With `cross_validate=True` it also compares institutions. |
| `compare_series` | Aligns several economies, institutions or concepts to a common frequency and computes correlations. When the same concept comes from different institutions, it cross-validates them automatically. |
| `calculate_statistics` | Computes descriptive statistics, growth, CAGR (for spans of a year or more), trend, volatility and maximum drawdown. Rates such as interest rates and inflation are compared in percentage points. |
| `explain_indicator` | Explains a concept and lists its sources and keys for one economy, or for all six default economies when none is given. For Korean statistics it adds the Bank of Korea glossary and methodology notes. |

There are three ways to ask for data:

1. **By concept**: `get_data(indicator="POLICY_RATE", country="US")`.
   - Sources are tried in catalog priority order. Korea uses ECOS first. If a source fails, returns nothing or hits a rate limit, the next one is used, and the attempts are recorded in the response.
   - `country` is required. Use an ISO code (`US`, `JPN`) or `EA` for the euro area.
2. **ECOS directly** (Korea only, so no `country` is needed): `get_data(stat_code="901Y009", item_code1="A01101")`
3. **SDMX directly**: `get_data(source="OECD", dataflow="OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)", key="JPN.M.IRLT.PA._Z._Z._Z._Z.N", cycle="M")`

Harmonisation options: `transform` (`yoy`/`pop` growth), `rebase_period` (rebase an index), `unit_mult` (rescale, e.g. `12` for trillions), `changes_only` (only the periods where the value changed).

---

## 📚 Concept Catalog (26 concepts)

- A **concept** defines *what* is measured and in *which canonical unit*.
- A **mapping** defines *where* it comes from: the dataflow, a key template (`{ISO2}`, `{ISO3}`, `{CUR}`), and the unit, scale and base period the source publishes. Mappings also record economies the source does not publish and dataflow-specific area codes.
- **No code was typed in by hand.** Every mapping was checked against the live API, and the search catalogs in `catalog/data/` are generated from the APIs (`scripts/build_catalogs.py`): 1,680 OECD/IMF/BIS dataflows and 1,349 ECOS items (e.g. rice `A01101`, gasoline `G02101`).

| Concept | Korea | JP · CN · EA · GB · US |
|---|---|---|
| `POLICY_RATE` policy rate | ECOS (daily, monthly) | BIS (daily, monthly) |
| `LONG_TERM_RATE` 10-year government bond yield | ECOS | OECD |
| `SHORT_TERM_RATE` 3-month rate | ECOS | OECD |
| `CPI` consumer price index | ECOS | IMF, BIS, OECD |
| `CPI_YOY` CPI inflation | ECOS (computed by the server) | IMF, BIS, OECD |
| `GDP_REAL_GROWTH_QOQ` / `_YOY` real GDP growth | ECOS | OECD |
| `GDP_REAL` / `GDP_NOMINAL` GDP level | ECOS | IMF (China: not seasonally adjusted) |
| `UNEMPLOYMENT_RATE` / `_SA` unemployment rate | ECOS | OECD (not China) |
| `CURRENT_ACCOUNT` / `GOODS_BALANCE` | ECOS (monthly) | IMF (quarterly) |
| `FX_RESERVES` official reserve assets | ECOS | IMF |
| `USD_EXCHANGE_RATE` per US dollar, monthly average | ECOS | IMF, BIS, OECD (not the US) |
| `USD_EXCHANGE_RATE_DAILY` per US dollar, daily | ECOS | BIS (not the US) |
| `SHARE_PRICE_INDEX` share prices | ECOS (KOSPI) | OECD |
| `CONSUMER_SENTIMENT` consumer confidence | ECOS (CCSI) | OECD (long-run average = 100) |
| `BUSINESS_CONFIDENCE` business confidence | OECD | OECD (long-run average = 100) |
| `HOUSE_PRICE_INDEX` residential property prices | ECOS (KB) | BIS |
| `PPI` producer price index | ECOS | IMF (US only among the defaults) |

Korea-only concepts keep national definitions that do not travel across countries: `M2`, `MONETARY_BASE`, `KTB_3Y`, `ECONOMIC_SENTIMENT` and `JEONSE_PRICE_INDEX`. Browse the full catalog in the `gesm://concepts` resource.

---

## 🔎 Validation layer

### Checks on every series

Every response carries these checks. Each result is `pass`, `info`, `warn` or `fail`.

| Check | What it verifies |
|---|---|
| `country` | The data is for the requested economy, which also catches providers that ignore a key filter. |
| `frequency` | The frequency matches and every period can be parsed. |
| `unit` | The unit is compatible with the concept, and an index has the expected base period. |
| `scale` | The unit multiplier (10^n) is as expected. |
| `period` | Periods are in order and inside the requested window. |
| `missing` | Missing values, gaps and periods not yet published. |
| `duplicate` | No period appears twice. |
| `revision` | Whether the provider revised values since the last retrieval (compared on raw values). |

### Cross-validation between institutions

Run it with `get_data(..., cross_validate=True)` or `compare_series`. The server fetches the same concept for the same economy from every institution. It aligns the frequency, scale and index base, then records each period's values and differences per institution.

When aligning frequencies, each concept aggregates the way its economics require: flows (current account, goods balance, GDP) are summed, stocks (FX reserves) take the end-of-period value, and rates, prices and indices are averaged. Averaging a monthly current account would understate the quarterly figure threefold. That bug was found by cross-validation and fixed.

Every comparison ends in one of three statuses. The system does not assume that official sources agree, and it records what nobody has explained yet:

| Status | Meaning |
|---|---|
| `MATCH` | Identical, or within tolerance (0.05pp for rates, 0.1% for indices or 1% after rebasing, 0.5% otherwise). |
| `DIFFER` | A real difference with a verifiable cause: different seasonal adjustment, the precision a provider publishes, or a documented known difference (`KNOWN_DIFFERENCES` in the catalog, added only with evidence). |
| `UNRESOLVED` | A real difference that nobody has explained yet. It is reported, never hidden and never silently resolved by picking one source. |
| `NOT_COMPARED` | Fewer than two sources, or no common unit, frequency or period. |

Each institution's result carries the difference and the investigation status:

```json
"OECD": {"vs": "IMF", "validation_status": "UNRESOLVED",
         "difference": {"absolute_max": 0.06751, "absolute_mean": 0.0225, "relative_max_pct": 2.2263},
         "investigation": {"status": "unresolved", "unresolved_periods": ["2026-01"]}}
```

Report of 2026-09-26 for the six default economies (last 12 months or quarters): **MATCH 25 · DIFFER 2 · UNRESOLVED 3 · NOT_COMPARED 1** out of 31 concept–economy pairs with two or more institutions.

| Concept · economy | Institutions | Status |
|---|---|---|
| Policy rate, 10-year and 3-month rates, unemployment (NSA and SA) · KR | ECOS vs BIS / OECD | MATCH, identical |
| Current account, goods balance, nominal GDP · KR | ECOS (monthly, summed) vs IMF (quarterly) | MATCH, identical |
| Real GDP, FX reserves, house prices · KR | ECOS vs IMF / BIS | MATCH |
| CPI · KR, US, JP, CN, GB | ECOS / IMF vs BIS vs OECD | MATCH after rebasing to a common period |
| CPI inflation · KR, US, CN | ECOS / IMF vs BIS vs OECD | MATCH |
| Exchange rate per USD, monthly · KR, JP, CN, EA, GB | ECOS / IMF vs BIS vs OECD | MATCH |
| Real GDP growth, year on year · KR | ECOS vs OECD | **DIFFER**: seasonal adjustment differs (ECOS NSA, OECD SA) |
| Consumer sentiment · KR | ECOS vs OECD | **DIFFER**: documented known difference (OECD amplitude-adjusted, CCSI not) |
| CPI inflation · GB | IMF vs BIS (vs OECD) | **UNRESOLVED**: BIS up to 0.32pp lower; an earlier run also had OECD up to 0.07pp apart |
| CPI inflation · JP | IMF vs BIS | **UNRESOLVED**: up to 0.16pp apart in 4 of 10 months |
| KRW per USD, daily · KR | ECOS vs BIS | **UNRESOLVED**: up to 28 won (2%) apart in recent days; not a one-day lag |
| KOSPI · KR | ECOS vs OECD | NOT_COMPARED: with the ECOS sample key only the latest 10 days come back, so there is no month in common |

Cross-validation also found two bugs before release. Averaging monthly flows understated quarterly current accounts, and a provider's `NaN` entered the canonical series as a number instead of a missing value. Both are fixed and covered by tests.

UNRESOLVED results are the kind of case the layer exists for: several official sources, different numbers, no documented reason yet. Each is recorded rather than silently picked.

### Validation ledger

Results are kept locally, bounded, and never committed:

```
$GESM_DATA_DIR/validation/
├── current/latest.json          latest status per institution pair and per series
└── history/YYYY-MM-DD.jsonl     every record of that day; deleted after GESM_LEDGER_RETENTION_DAYS (default 90)
```

The `gesm://validation/summary` resource shows, per concept, economy and institution pair, the agreement rate and the MATCH / DIFFER / UNRESOLVED counts over the retention window, plus the latest status. This is the basis for evaluating how reliable a statistical MCP is. Revision snapshots are kept under `revisions/`.

### Daily cross-validation report

`scripts/validation_report.py` cross-validates every concept that two or more institutions publish for the six default economies (31 concept–economy pairs) over the last 12 months or quarters. It writes `summary.json` and `summary.md`. CI runs it daily, shows the table in the job summary and keeps it as a 90-day artifact; raw records are not uploaded.

## 🧾 Provenance

Every series carries:

- agency, dataset, series key and retrieval time
- the query URL (the ECOS key is masked as `{API_KEY}`)
- the transformations applied (e.g. a computed yoy, a rebase, a declared unit)
- a ready-to-use `citation`

`output_format="sdmx"` returns an SDMX-JSON 2.1 data message; a test validates it against the official schema.

---

## 🚀 Installation

Check connectivity first ([uv](https://docs.astral.sh/uv/) required):

```bash
ECOS_API_KEY=your_key uvx --from git+https://github.com/kgy0617/global-economic-statistical-mcp global-economic-statistical-mcp --check
```

**Claude Desktop** (`claude_desktop_config.json`) / **Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "global-econ-stats": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/kgy0617/global-economic-statistical-mcp", "global-economic-statistical-mcp"],
      "env": { "ECOS_API_KEY": "your_api_key_here" }
    }
  }
}
```

**Claude Code**

```bash
claude mcp add global-econ-stats -e ECOS_API_KEY=your_key -- uvx --from git+https://github.com/kgy0617/global-economic-statistical-mcp global-economic-statistical-mcp
```

| Environment variable | Description |
|---|---|
| `ECOS_API_KEY` | ECOS key ([free registration](https://ecos.bok.or.kr/api/#/)). Without it the sample key is used (10 rows per call). OECD, IMF and BIS need no key. |
| `GESM_DATA_DIR` | Where the validation ledger and revision snapshots are stored (default `~/.cache/global-economic-statistical-mcp`). |
| `GESM_PERSIST` | Set to `0` to write nothing to disk. |
| `GESM_LEDGER_RETENTION_DAYS` | Days of validation history to keep (default 90). |

---

## 📖 Research examples

- "How has monetary policy diverged across the US, the euro area and Japan?" → `compare_series(series=[{"indicator":"POLICY_RATE","country":"US"}, {"indicator":"POLICY_RATE","country":"EA"}, {"indicator":"POLICY_RATE","country":"JP"}])`
- "Is inflation in the UK still above the euro area?" → `compare_series(series=[{"indicator":"CPI_YOY","country":"GB"}, {"indicator":"CPI_YOY","country":"EA"}])`
- "Do the IMF, BIS and OECD agree on China's CPI?" → `get_data(indicator="CPI", country="CN", cross_validate=True)`
- "Current-account balances of the six largest economies" → `get_data(indicator="CURRENT_ACCOUNT", country=...)` for each, or `compare_series`
- "Trend and volatility of Japanese 10-year yields" → `calculate_statistics(indicator="LONG_TERM_RATE", country="JP")`
- "What exactly is the OECD consumer confidence indicator?" → `explain_indicator(term="CONSUMER_SENTIMENT", country="GB")`
- "Structure of the IMF balance of payments dataset" → `get_metadata(source="IMF", dataflow="IMF.STA:BOP", code_keyword="current account")`
- Korean micro-data such as the price of rice → `search_statistics(query="쌀", scope="items")`, then `get_data(stat_code="901Y009", item_code1="A01101", transform="yoy")`

Prompts: `macro-economic-briefing`, `compare-countries`, `analyze-economic-trend`.

Tool descriptions, errors and field names are in English. Korean appears only where it is data: Korean statistic names (`name_ko`), ECOS labels, and Korean search terms, which work as queries.

---

## 🧪 Development

```bash
git clone https://github.com/kgy0617/global-economic-statistical-mcp.git
cd global-economic-statistical-mcp
uv sync
```

| Command | Description |
|---|---|
| `uv run pytest` | Offline tests. ECOS, OECD, IMF and BIS are all replaced by fake servers. |
| `uv run pytest -m live` | Re-verifies every catalog mapping for KR, US, JP, CN, EA and GB against the live APIs, and validates SDMX-JSON output against the official schema. OECD mappings are checked for all six economies in one request each; a rate-limited request waits out the cooldown once, then is skipped with the reason. |
| `uv run python scripts/validation_report.py` | Cross-validates all 31 multi-institution concept–economy pairs and writes the summary report. |
| `uv run python scripts/build_catalogs.py` | Regenerates the dataflow and ECOS item indexes from the APIs. |
| `uv run python scripts/update_tables.py` | Regenerates the ECOS table list. |

Source layout (`src/global_economic_statistical_mcp/`)

| File | Role |
|---|---|
| `catalog/concepts.py` | Concept Catalog |
| `catalog/countries.py` | Economies, provider area codes, default set |
| `catalog/search.py` | Search catalogs |
| `providers/ecos.py` | ECOS adapter |
| `providers/sdmx_rest.py` | OECD, IMF and BIS adapter |
| `model.py` | Canonical Model |
| `validation.py` | Validation layer |
| `storage.py` | Validation ledger and revision snapshots |
| `service.py` | Concept and provider resolution, orchestration |
| `formatting.py` | Output formats and provenance |
| `ecos_sdmx.py` | ECOS → SDMX mapping |
| `server.py` | MCP tools, resources and prompts |

Adding an institution means implementing the `Provider` protocol in `providers/base.py` (or adding an `SdmxSource` for another SDMX endpoint) and mapping concepts to it in the catalog.

## 🗺️ Roadmap

The priority is to make what exists trustworthy, not to add breadth. Planned next:

- **Second-tier indicators**: PPI beyond Korea and the US, core CPI, industrial production, retail sales, PMI.
- **Investigate UNRESOLVED differences** and record confirmed causes as documented known differences.
- **More institutions** when a concept needs them: ECB and Eurostat (SDMX-CSV), World Bank (SDMX-ML).
- **Currency normalisation** designed as a feature (spot vs period average vs period end, PPP), rather than a helper function.

## ⚠️ Limitations

- Only the six default economies are re-verified. Others resolve through the same templates, and their validation reports show the outcome.
- Institutions differ in methods, base years and revisions. Cross-validation shows where they differ; it does not decide which one is right.
- Values are verified for the last year or so. Older history is served as published, without cross-validation.
- The ECOS → SDMX structure mapping is this server's own mapping, not an official one, and the output says so.
- OECD's rate limit is per IP. Heavy use can put OECD on cooldown, and the resolver then falls back to other institutions where the catalog has them.

## 📝 License

MIT License. Release notes are in [CHANGELOG.md](CHANGELOG.md).
