# Sources by concept

Which institution serves each concept for each default economy. Ask by concept
(`get_data(indicator=..., country=...)`) and the server picks the source in this priority order;
add `source="..."` to choose one yourself. For the euro area always use `country="EA"`.

| Concept | Korea | JP · CN · GB · US | Euro area |
|---|---|---|---|
| `POLICY_RATE` | ECOS (daily, monthly) | BIS (daily, monthly) | BIS, ECB (deposit facility rate) |
| `LONG_TERM_RATE` | ECOS | OECD | OECD, ECB |
| `SHORT_TERM_RATE` | ECOS | OECD | OECD, ECB (Euribor) |
| `CPI` | ECOS | IMF, BIS, OECD (not JP) | Eurostat, ECB (HICP), BIS |
| `CPI_YOY` | ECOS (computed by the server) | IMF, BIS, OECD (not JP) | Eurostat, ECB, BIS |
| `GDP_REAL_GROWTH_QOQ` / `_YOY` | ECOS | OECD | OECD, Eurostat |
| `GDP_REAL` / `GDP_NOMINAL` | ECOS | IMF (China NSA), World Bank (annual) | IMF, Eurostat |
| `UNEMPLOYMENT_RATE` / `_SA` | ECOS | OECD (not China) | OECD, Eurostat |
| `CURRENT_ACCOUNT` | ECOS (monthly) | IMF (quarterly), World Bank (annual) | IMF, World Bank |
| `GOODS_BALANCE` | ECOS (monthly) | IMF (quarterly) | IMF |
| `FX_RESERVES` | ECOS | IMF, World Bank (annual) | IMF, World Bank |
| `USD_EXCHANGE_RATE` | ECOS | IMF, BIS, OECD, World Bank (not the US) | IMF, BIS, OECD, ECB, World Bank |
| `USD_EXCHANGE_RATE_DAILY` | ECOS | BIS (not the US) | BIS, ECB |
| `SHARE_PRICE_INDEX` | ECOS (KOSPI) | OECD | OECD |
| `CONSUMER_SENTIMENT` | ECOS (CCSI) | OECD | OECD |
| `BUSINESS_CONFIDENCE` | OECD | OECD | OECD |
| `HOUSE_PRICE_INDEX` | ECOS (KB) | BIS | BIS, Eurostat |
| `PPI` | ECOS | IMF (US only among the defaults) | — |
| `GDP_REAL_GROWTH_ANNUAL`, `CPI_INFLATION_ANNUAL`, `POPULATION`, `GDP_PER_CAPITA_PPP`, `CURRENT_ACCOUNT_GDP` | World Bank | World Bank | World Bank (not `CURRENT_ACCOUNT_GDP`) |

Korea only: `M2`, `MONETARY_BASE`, `KTB_3Y`, `ECONOMIC_SENTIMENT`, `JEONSE_PRICE_INDEX`.

Good to know:

- ECB and Eurostat cover the euro area and EU member states only. For a member state's policy rate,
  use `country="EA"`.
- World Bank indicators are annual and published with a lag, so the latest year is often missing.
- OECD limits requests per IP. Under heavy use it pauses for a minute and the server falls back to
  other sources.
- `explain_indicator(term=...)` shows a concept's sources and keys for every default economy.
