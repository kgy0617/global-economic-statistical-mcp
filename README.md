# 🌏 Global Economic Statistical MCP

한국은행 **ECOS**와 **OECD · IMF · BIS**(SDMX)의 거시경제 통계를 하나의 개념 체계로 묶는 MCP 서버입니다.

LLM이 경제통계를 **발견**하고, **의미를 해석**하고, 여러 기관의 통계를 **같은 모델로 조회·검증·비교**할 수 있게 합니다. 모든 응답에는 출처(provenance)와 검증 결과(validation)가 붙습니다.

```
                 ┌────────────────────┐
                 │        LLM         │
                 └─────────┬──────────┘
                           ▼
                 ┌────────────────────┐
                 │  Statistical MCP   │   6 MCP tools
                 └─────────┬──────────┘
                  ┌────────▼────────┐
                  │ Concept Resolver│
                  └────────┬────────┘
             ┌─────────────┴─────────────┐
       Concept Catalog             Provider Catalog
             └─────────────┬─────────────┘
                  Provider Resolver
          ┌────────────────┼────────────────┐
        ECOS             SDMX            (Future)
     (REST, 한국)    ┌─────┼─────┐
                    OECD   IMF   BIS
                          ▼
                   Canonical Model
                          ▼
                     Validation
                          ▼
                      Analysis
                          ▼
                     Provenance
```

이 프로젝트에서 가장 중요한 것은 MCP 서버 자체가 아니라 아래 네 가지입니다. MCP는 이것들을 LLM에 노출하는 인터페이스 역할을 합니다.

- **Concept Catalog**: 국가와 무관한 표준 개념과, 기관별 시계열 매핑
- **Canonical Model**: 모든 기관의 데이터를 담는 공통 시계열 모델
- **Validation**: 검증 계층과 검증 원장(ledger)
- **Provenance**: 출처와 가공 이력

---

## 🏛️ 연동 기관

한국은행은 SDMX 엔드포인트가 없어 ECOS REST API로 연동하고, 나머지 기관은 SDMX로 연동합니다. 한국은행 외에는 모두 키가 필요 없습니다.

| 기관 | 엔드포인트 | 방식 | 확인된 특이사항 |
|---|---|---|---|
| **ECOS** (한국은행) | `ecos.bok.or.kr/api` | ECOS REST | `ECOS_API_KEY`가 필요합니다(없으면 sample 키, 1회 10건). 구조는 서버가 SDMX 개념으로 매핑합니다. |
| **OECD** | `sdmx.oecd.org/public/rest` | SDMX 2.1 · JSON 2.0 | **IP당 호출 한도(HTTP 429)가 엄격합니다.** 그래서 동시 요청을 1개로 제한하고, 한도를 넘으면 해당 기관을 잠시 차단한 뒤 다른 출처로 자동 전환합니다. |
| **IMF** | `api.imf.org/external/sdmx/3.0` | SDMX 3.0 · JSON 2.0 | 예전 API(dataservices.imf.org)는 폐기되었습니다. **키에 `*`가 있으면 국가 필터까지 무시**하므로 받은 결과를 다시 걸러냅니다. 기준연도는 국가마다 다릅니다(한국 2020, 미국 2010). |
| **BIS** | `stats.bis.org/api/v1`(데이터) · `/v2`(구조) | SDMX 2.1 · 3.0 | 정책금리와 환율에는 단위가 표기되지 않아 카탈로그에 선언한 단위를 씁니다(검증 결과에 `info`로 표시). |

---

## 🧰 MCP Tools (6개)

| 도구 | 역할 |
|---|---|
| `search_statistics` | 표준 개념, ECOS 통계표와 세부 품목, OECD·IMF·BIS 데이터플로, 한국은행 100대 지표를 검색합니다. |
| `get_metadata` | ECOS 통계표 구조(SDMX 매핑, 연결된 표준 개념과 해외 출처 포함)나 OECD·IMF·BIS가 발행한 실제 DSD와 코드목록을 조회합니다. |
| `get_data` | 개념+국가(`indicator="CPI_YOY", country="US"`), ECOS 통계표, SDMX 데이터플로를 조회합니다. 검증과 출처가 붙고, `cross_validate=True`면 기관 간 교차검증도 합니다. |
| `compare_series` | 여러 국가·기관·지표를 같은 주기로 맞춰 정렬하고 상관계수를 계산합니다. 같은 개념을 다른 기관에서 가져오면 교차검증도 자동으로 합니다. |
| `calculate_statistics` | 기술통계, 증감률, CAGR(1년 이상일 때), 추세, 변동성, 최대낙폭을 계산합니다. 금리·물가상승률 같은 비율 지표는 %p 기준으로 계산합니다. |
| `explain_indicator` | 개념 정의(한/영), 국가별 출처와 키, 한국은행 용어사전과 통계 설명자료를 보여줍니다. |

데이터는 세 가지 방법으로 조회할 수 있습니다.

1. **표준 개념**: `get_data(indicator="POLICY_RATE", country="US")`
   - 한국은 ECOS가 1순위이고, 다른 나라는 카탈로그의 우선순위를 따릅니다.
2. **ECOS 직접 조회**: `get_data(stat_code="901Y009", item_code1="A01101")`
3. **SDMX 직접 조회**: `get_data(source="OECD", dataflow="OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)", key="JPN.M.IRLT.PA._Z._Z._Z._Z.N", cycle="M")`

값을 맞추는 옵션도 있습니다: `transform`(yoy/pop 증감률), `rebase_period`(지수 재기준), `unit_mult`(배수 환산, 예: 십억원 → 조원은 `12`), `changes_only`(값이 바뀐 시점만).

첫 출처에 데이터가 없거나 오류(호출 한도 초과 포함)가 나면 다음 출처로 자동 전환하고, 그 시도 기록을 응답에 남깁니다.

---

## 📚 Concept Catalog (25개 개념)

- **개념**은 *무엇을* *어떤 단위로* 재는지를 정의합니다.
- **매핑**은 *어디서* 가져오는지를 정의합니다: 데이터플로, `{ISO2}`·`{ISO3}`·`{CUR}` 키 템플릿, 공급자가 발표하는 단위와 기준.
- **모든 매핑은 실제 API로 검증했습니다.** `uv run pytest -m live`를 실행하면 한국과 미국에 대해 매번 다시 확인합니다.

| 개념 | 한국 | 다른 국가 |
|---|---|---|
| `POLICY_RATE` 정책금리 | ECOS(일·월) | BIS(일·월) |
| `CPI` 소비자물가지수 | ECOS | IMF, OECD |
| `CPI_YOY` 물가상승률 | ECOS(서버 계산) | IMF, OECD |
| `GDP_REAL_GROWTH_QOQ` / `_YOY` 성장률 | ECOS | OECD |
| `UNEMPLOYMENT_RATE` / `_SA` 실업률 | ECOS | OECD |
| `USD_EXCHANGE_RATE` 대미달러 환율(월평균) | ECOS | IMF, BIS, OECD |
| `LONG_TERM_RATE` 10년 국채 · `SHORT_TERM_RATE` 3개월 금리 | ECOS | OECD |
| `SHARE_PRICE_INDEX` 주가지수 | ECOS(KOSPI) | OECD |
| `HOUSE_PRICE_INDEX` 주택가격 | ECOS(KB) | BIS |

한국 전용 개념(ECOS)도 있습니다: `PPI`, `GDP_REAL`, `GDP_NOMINAL`, `M2`, `MONETARY_BASE`, `CURRENT_ACCOUNT`, `GOODS_BALANCE`, `FX_RESERVES`, `CONSUMER_SENTIMENT`, `ECONOMIC_SENTIMENT`, `KTB_3Y`, `JEONSE_PRICE_INDEX`, `USD_EXCHANGE_RATE_DAILY`. 전체 목록은 리소스 `gesm://concepts`에서 볼 수 있습니다.

검색에 쓰는 카탈로그(`catalog/data/`)는 모두 API에서 생성합니다(`scripts/build_catalogs.py`). 사람이 직접 입력한 코드는 없습니다.

- OECD·IMF·BIS 데이터플로 1,680개
- 자주 쓰는 ECOS 통계표의 세부 품목 1,349개 (예: 쌀 `A01101`, 휘발유 `G02101`)

---

## 🔎 Validation Layer

### 단일 시계열 검사

모든 응답에 포함되며, 각 검사 결과는 `pass` / `info` / `warn` / `fail` 중 하나입니다.

| 검사 | 내용 |
|---|---|
| `country` | 요청한 국가의 데이터인지 확인합니다. 공급자가 필터를 무시한 경우도 잡아냅니다. |
| `frequency` | 주기가 일치하고, 모든 시점을 해석할 수 있는지 확인합니다. |
| `unit` | 단위가 개념과 호환되는지, 지수의 기준시점이 기대와 같은지 확인합니다. |
| `scale` | 단위 배수(10^n)가 기대와 같은지 확인합니다. |
| `period` | 시점이 시간순이고 요청 기간 안에 있는지 확인합니다. |
| `missing` | 결측값, 중간 누락 시점, 아직 발표되지 않은 시점을 찾습니다. |
| `duplicate` | 같은 시점이 두 번 나오지 않는지 확인합니다. |
| `revision` | 지난 조회 이후 공급자가 값을 개정했는지 확인합니다(원자료 기준으로 비교). |

### 기관 간 교차검증

`get_data(..., cross_validate=True)`나 `compare_series`로 실행합니다. 같은 개념·국가를 여러 기관에서 가져와 주기, 배수, 지수 기준을 맞춘 뒤, 시점별 값과 차이를 기록합니다. 아래는 2026-09-26 실제 결과입니다.

```json
{"concept_id":"CPI","country":"KR","period":"2026-08","unit":"IX","frequency":"M",
 "method":"rebased:2025-11=100",
 "values":{"ECOS":102.431741,"IMF":102.431741,"OECD":102.431742},
 "by_provider":{"IMF":{"difference":0.0,"status":"match"},"OECD":{"difference":1e-06,"status":"within_tolerance"}}}
```

| 개념(한국) | 결과 |
|---|---|
| 정책금리: ECOS vs BIS | 7개월 모두 **일치** |
| 실업률(계절조정): ECOS vs OECD | 6개월 모두 **일치** |
| 10년 국채: ECOS vs OECD | 6개월 모두 **일치** |
| 소비자물가지수: ECOS vs IMF | **일치**. OECD는 2015 기준이라 재기준화한 뒤 비교했고, 허용 오차 이내 |
| 물가상승률: ECOS(서버 계산) vs IMF·OECD | 차이 약 0.00004%p |
| 원/달러 월평균: ECOS vs IMF | **일치**. BIS·OECD는 집계 방식이 달라 최대 0.44% 차이 |

- **검증 원장**: 결과는 `$GESM_DATA_DIR/validation_ledger.jsonl`에 쌓이고, 리소스 `gesm://validation/summary`에서 기관 쌍별 일치율을 볼 수 있습니다.
- **개정 이력**: 스냅샷이 `revisions/`에 저장됩니다.

## 🧾 Provenance

모든 시계열에는 다음 정보가 붙습니다.

- 기관, 데이터셋, 시계열 키, 조회 시각
- 조회 URL (ECOS 키는 `{API_KEY}`로 가려짐)
- 가공 이력 (예: yoy 계산, 재기준화, 단위 선언)
- 인용문(`citation`)

`output_format="sdmx"`는 SDMX-JSON 2.1 data message를 반환하며, 공식 스키마로 검증하는 테스트가 있습니다.

---

## 🚀 설치와 설정

먼저 진단 명령으로 연결 상태를 확인합니다([uv](https://docs.astral.sh/uv/) 필요).

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

| 환경변수 | 설명 |
|---|---|
| `ECOS_API_KEY` | ECOS 인증키([무료 발급](https://ecos.bok.or.kr/api/#/)). 없으면 sample 키(1회 10건)를 씁니다. |
| `GESM_DATA_DIR` | 검증 원장과 개정 스냅샷을 저장할 위치 (기본값 `~/.cache/global-economic-statistical-mcp`) |
| `GESM_PERSIST` | `0`이면 디스크에 기록하지 않습니다. |

---

## 📖 사용 예시

- "미국 기준금리 알려줘" → `get_data(indicator="POLICY_RATE", country="US")` (BIS `M.US`)
- "한국 물가 통계가 기관마다 같아?" → `get_data(indicator="CPI", cross_validate=True)`
- "한·미·일 장기금리 비교" → `compare_series(series=[{"indicator":"LONG_TERM_RATE","country":"KR"}, {"indicator":"LONG_TERM_RATE","country":"US"}, {"indicator":"LONG_TERM_RATE","country":"JP"}])`
- "쌀값 상승률" → `search_statistics(query="쌀", scope="items")`로 코드를 찾은 뒤 `get_data(stat_code="901Y009", item_code1="A01101", transform="yoy")`
- "IMF CPI 데이터셋 구조" → `get_metadata(source="IMF", dataflow="IMF.STA:CPI", code_keyword="KOR")`

제공 프롬프트: `macro-economic-briefing`, `compare-countries`, `analyze-economic-trend`

---

## 🧪 개발

```bash
git clone https://github.com/kgy0617/global-economic-statistical-mcp.git
cd global-economic-statistical-mcp
uv sync
```

| 명령 | 설명 |
|---|---|
| `uv run pytest` | 오프라인 테스트. ECOS·OECD·IMF·BIS를 모두 가짜 서버로 대체합니다. |
| `uv run pytest -m live` | 실제 API로 카탈로그 매핑 전체를 다시 검증하고, SDMX-JSON 출력을 공식 스키마로 검증합니다. OECD 호출 한도를 넘으면 해당 테스트는 사유와 함께 skip됩니다. |
| `uv run python scripts/build_catalogs.py` | 데이터플로 색인과 ECOS 품목 색인을 다시 만듭니다. |
| `uv run python scripts/update_tables.py` | ECOS 통계표 목록을 다시 만듭니다. |

소스 구조 (`src/global_economic_statistical_mcp/`)

| 파일 | 역할 |
|---|---|
| `catalog/concepts.py` | Concept Catalog |
| `catalog/search.py` | 검색 카탈로그 |
| `catalog/countries.py` | 국가 코드 |
| `providers/ecos.py` | ECOS 어댑터 |
| `providers/sdmx_rest.py` | OECD·IMF·BIS 어댑터 |
| `model.py` | Canonical Model |
| `validation.py` | Validation 계층 |
| `storage.py` | 검증 원장과 개정 스냅샷 |
| `service.py` | 개념·출처 리졸버와 전체 흐름 조율 |
| `formatting.py` | 출력 형식과 Provenance |
| `ecos_sdmx.py` | ECOS → SDMX 매핑 |
| `server.py` | MCP 도구 |

## 📝 라이선스

MIT License
