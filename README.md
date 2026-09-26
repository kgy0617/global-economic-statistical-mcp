# 🌏 Global Economic Statistical MCP

**Global economic statistics infrastructure for AI-powered macro research.**

An MCP server that lets an LLM find, retrieve, compare and analyse official macroeconomic statistics from central banks and international organisations. You ask for a *concept* (policy rate, CPI inflation, real GDP, current account, …) for an *economy*. The server fetches it from the right institution, converts it to one common time-series format, checks it against other institutions, and cites its source.

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
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
     ECOS              SDMX Layer           Data360
  (REST, Korea)            │               (World Bank)
       │   ┌──────┬──────┼──────┬──────┐       │
       │   ▼      ▼      ▼      ▼      ▼       │
       │  OECD   IMF    BIS    ECB  Eurostat   │
       │   │      │      │      │      │       │
       └───┴──────┴──────┴──────┴──────┴───────┘
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

---

## 🚀 Installation

Requires [uv](https://docs.astral.sh/uv/). Check connectivity first:

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
| `ECOS_API_KEY` | Bank of Korea key ([free registration](https://ecos.bok.or.kr/api/#/)). Without it a sample key is used (10 rows per call). No other institution needs a key. |
| `GESM_DATA_DIR` | Where validation results are stored (default `~/.cache/global-economic-statistical-mcp`). |
| `GESM_PERSIST` | `0` writes nothing to disk. |

---

## 🌐 Coverage

**Economies**: Korea `KR`, United States `US`, Japan `JP`, China `CN`, euro area `EA`, United Kingdom `GB`. Every mapping for these six is re-checked against the live APIs daily. About 40 more economies work through the same mappings without daily checks (list: `gesm://countries`).

**Institutions**

| `source` | Institution | Covers |
|---|---|---|
| `ECOS` | Bank of Korea | Korea (the only source that needs a key) |
| `OECD` | OECD | OECD members and major economies |
| `IMF` | IMF | Most countries |
| `BIS` | Bank for International Settlements | Policy rates, exchange rates, prices, property |
| `ECB` | European Central Bank | Euro area and EU member states |
| `EUROSTAT` | Eurostat | Euro area and EU member states |
| `WB` | World Bank (Data360) | Most countries; annual development indicators |

Not every institution publishes every concept. See [docs/sources.md](docs/sources.md) for which institution serves what.

---

## 🧰 Tools

| Tool | What it does |
|---|---|
| `search_statistics` | Find concepts, datasets and indicators across all institutions. |
| `get_metadata` | A dataset's structure and codes, or an indicator's definition. |
| `get_data` | Retrieve a series, with validation and a citation. `cross_validate=True` compares institutions. |
| `compare_series` | Align several economies, institutions or concepts and compute correlations. |
| `calculate_statistics` | Growth, trend, volatility, drawdown and other summary statistics. |
| `explain_indicator` | What a concept means and which sources serve each economy. |

Ask by concept; `country` is required:

```python
get_data(indicator="CPI_YOY", country="EA")                 # the server picks the source
get_data(indicator="POLICY_RATE", country="US", source="BIS")  # or choose one
```

You can also query an institution directly (`source` + `dataflow` + `key`) or a Bank of Korea table (`stat_code`). Options: `transform` (`yoy`/`pop`), `rebase_period`, `unit_mult`, `start_date`/`end_date`.

**31 concepts**: policy, short- and long-term rates; CPI and inflation; PPI; GDP growth and levels; unemployment; current account, goods balance, FX reserves; exchange rates; share prices; consumer and business confidence; house prices; and annual population, GDP per capita (PPP) and current account/GDP. Full list: `gesm://concepts`.

---

## 🔎 Validation

Every series comes with checks on country, frequency, unit, scale, period, missing values, duplicates and revisions.

With `cross_validate=True` (or `compare_series`), the same concept is fetched from every institution that publishes it and compared period by period. Each result is one of:

| Status | Meaning |
|---|---|
| `MATCH` | The institutions agree (within tolerance). |
| `DIFFER` | They differ for a known reason (e.g. seasonal adjustment). |
| `UNRESOLVED` | They differ and the cause is not known. Reported, never hidden. |
| `NOT_COMPARED` | Not enough data to compare. |

Official sources do not always agree. Latest report for the six economies: **MATCH 43 · DIFFER 3 · UNRESOLVED 15 · NOT_COMPARED 1**. See [docs/validation.md](docs/validation.md) for the details.

## 🧾 Provenance

Every series carries its institution, dataset, series key, retrieval time, query URL, any transformation applied, and a ready-to-use `citation`. `output_format="sdmx"` returns an SDMX-JSON data message.

---

## 📖 Examples

- "How has monetary policy diverged across the US, the euro area and Japan?" → `compare_series(series=[{"indicator":"POLICY_RATE","country":"US"}, {"indicator":"POLICY_RATE","country":"EA"}, {"indicator":"POLICY_RATE","country":"JP"}])`
- "Do Eurostat, the ECB and the BIS agree on euro-area inflation?" → `get_data(indicator="CPI_YOY", country="EA", cross_validate=True)`
- "Germany's unemployment rate" → `get_data(indicator="UNEMPLOYMENT_RATE_SA", country="DE", source="Eurostat")`
- "Trend and volatility of Japanese 10-year yields" → `calculate_statistics(indicator="LONG_TERM_RATE", country="JP")`
- "GDP per capita (PPP) over 20 years" → `get_data(indicator="GDP_PER_CAPITA_PPP", country="CN", recent_years=20)`

Prompts: `macro-economic-briefing`, `compare-countries`, `analyze-economic-trend`.

## ⚠️ Limitations

- Institutions differ in methods, base years and revisions. Cross-validation shows where they differ; it does not decide which one is right.
- World Bank indicators are annual and published with a lag.
- OECD limits requests per IP; under heavy use the server falls back to other institutions.
- Only the six default economies are checked daily.

---

## 🧪 Development

```bash
git clone https://github.com/kgy0617/global-economic-statistical-mcp.git
cd global-economic-statistical-mcp
uv sync
uv run pytest              # offline tests (all institutions faked)
uv run pytest -m live      # live API checks for the six economies
```

License: MIT.
