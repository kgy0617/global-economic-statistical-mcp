# 🏦 ECOS MCP Server

한국은행 경제통계시스템(ECOS) Open API를 위한 차세대 MCP(Model Context Protocol) 통계 서버입니다.

단순히 API를 래핑하는 수준을 넘어 **“SDMX 표준 개념체계 연결 → 품목 단위 의미 검색 → 정규화(Harmonization) 분석 → 출처 근거(Evidence) 제공”**의 완전한 통계 데이터 파이프라인을 제공합니다.

```
                 ┌────────────────────────────────────────────────────────┐
                 │                      LLM / Agent                       │
                 └───────────────────────────┬────────────────────────────┘
                                             │ MCP
                                             ▼
                 ┌────────────────────────────────────────────────────────┐
                 │                    ECOS MCP Server                     │
                 │                                                        │
                 │  [검색·개념] search_statistics (품목/동의어/SDMX 개념)   │
                 │  [구조·DSD] get_metadata (차원/코드/국제기구 매핑)       │
                 │  [데이터]   get_data (단위변환/기준연도 재지수화/출처근거) │
                 │  [비교분석] compare_series / calculate_statistics      │
                 │  [지표해설] explain_indicator (용어사전/해설서)        │
                 └───────────┬────────────────────────────────┬───────────┘
                             │                                │
                             ▼                                ▼
              ┌──────────────────────────────┐ ┌──────────────────────────────┐
              │          ECOS API            │ │      SDMX Semantic Layer     │
              │  - 통계표/시계열 원천 데이터 │ │  - Canonical Concepts (23종) │
              │  - 일/월/분기/연 주기 통계   │ │  - SDMX DSD / Codelist       │
              │  - 100대 통계지표 / 용어사전 │ │  - IMF/OECD/BIS 연계 슬롯    │
              └──────────────────────────────┘ └──────────────────────────────┘
                                             │
                                             ▼
                             ┌────────────────────────────────┐
                             │  출처 근거(Evidence Citation)  │
                             │  - 출처 기관 및 통계표 코드     │
                             │  - 시계열 키 및 KST 조회 시각   │
                             │  - ECOS 원문 링크 및 표준 인용문│
                             └────────────────────────────────┘
```

ECOS와 SDMX는 명확히 분업합니다: **ECOS API는 값과 코드의 원천**이며, **SDMX 레이어는 지표를 글로벌 표준 개념으로 정규화**하여 LLM이 전 세계 통계체계와 일관되게 해석할 수 있도록 보장합니다.

---

## 🌟 핵심 차별점

1. **SDMX 표준 개념 연결 계층 (Canonical Concept Layer)**:
   - 한국은행 주요 23개 지표(`BOK_BASE_RATE`, `CPI_HEADLINE`, `GDP_REAL_GROWTH`, `EXR_USD_KRW`, `MONEY_M2`, `BOND_KTB_3Y` 등)를 표준 SDMX 개념으로 매핑.
   - 표준 차원 및 속성(`REF_AREA=KR`, `FREQ`, `UNIT_MEASURE`, `UNIT_MULT`, `BASE_PER`, `DECIMALS`) 완비.
   - IMF, OECD, BIS, FRED 등 국제기구 표준 데이터셋과의 연계 식별자(Mapping hooks) 제공.
2. **품목 단위 세부 검색 & 의미 검색 (Item-level & Semantic Search)**:
   - 통계표 제목뿐만 아니라 **"쌀"**, **"휘발유"**, **"반도체"**, **"전기료"**, **"사과"**, **"아파트"**, **"국고채 10년"** 등 실물 경제 세부 품목 직접 검색.
   - `"inflation"`, `"interest rate"` 등 영문·한글 동의어 및 표준 개념 기반 검색.
   - 검색 결과에서 즉시 실행 가능한 `get_data_example` 제공.
3. **출처 근거 블록 (Evidence Citation Block)**:
   - 모든 데이터 응답에 `source_agency`, `table_name`, `table_code`, `series_key`, `retrieved_at`(KST), `ecos_url`, `citation`, `sdmx_concept` 포함.
   - CSV 헤더 주석(`# evidence_...`, `# citation: ...`) 및 JSON/Compact 블록 제공으로 LLM의 할루시네이션 방지 및 보고서 신뢰도 극대화.
4. **데이터 정규화 & 조화 (Data Harmonization)**:
   - **단위·배수 정규화(`unit_mult`)**: 원, 십억원, 조원 등 서로 다른 배수를 $10^k$ 지수 단위로 표준화.
   - **기준연도 재지수화(`rebase_period`)**: 기준년도가 다른 지수(예: 2015=100 vs 2020=100)를 특정 시점(100) 기준으로 재계산하여 비교 분석 지원.

---

---

## ✨ MCP Tools (6개)

모든 도구는 읽기 전용(`readOnlyHint`)으로 표시되어 있으며, 실패하면 MCP 표준 에러(`isError: true`)를 반환합니다.

| # | Tool | 역할 | 주요 특징 |
|:-:|------|------|-----------|
| ① | `search_statistics` | **검색·탐색** | 인기 지표 프리셋과 통계표(Dataflow)를 로컬 인덱스에서 즉시 검색하고, 분류 트리 탐색과 100대 주요 지표(`scope="key_statistics"`)를 지원 |
| ② | `get_metadata` | **구조 이해** | 통계표를 SDMX DSD로 매핑. 차원(FREQ·ITEM_CODE1~4), 항목코드, 주기별 수록기간, 단위를 확인하고 `get_data_example` 제공 |
| ③ | `get_data` | **데이터 조회** | 인기 지표 1-shot(`indicator="기준금리"`), 최신 구간 우선, 증감률(`transform`), 유연한 날짜 입력, compact/csv/json/**sdmx** 포맷 |
| ④ | `compare_series` | **비교·상관** | 2~6개 계열을 공통 주기로 집계·정렬하고 쌍별 상관계수, 지수화(`index`)·표준화(`zscore`) 제공 |
| ⑤ | `calculate_statistics` | **요약통계** | 평균·중앙값·표준편차, 기간 변화율, CAGR, 선형추세(기울기·R²), 변동성, 최대낙폭, 최근 전년동기비 |
| ⑥ | `explain_indicator` | **지표 설명** | 통계용어사전 정의, 통계 설명자료(작성기관·주기·작성방법), 관련 프리셋·통계표, SDMX 구조 요약을 한 번에 |

권장 흐름: `search_statistics` → `get_metadata` → `get_data`. 인기 지표는 `get_data(indicator=...)` 하나로 끝납니다.

---

## 🧭 SDMX 레이어

한국은행은 ECOS용 SDMX 서비스를 제공하지 않습니다. 그래서 이 서버가 **ECOS 메타데이터를 SDMX 정보모델로 직접 매핑**합니다. 관리 기관(agencyID)은 `ECOS_MCP`이며, 모든 SDMX 산출물에 비공식 매핑이라는 주석(annotation)이 붙습니다.

| ECOS | SDMX |
|------|------|
| 통계표 `STAT_CODE` | Dataflow `{STAT_CODE}` + DSD `DSD_{STAT_CODE}` |
| 주기 `A/S/Q/M/SM/D` | 차원 `FREQ`, 코드리스트 `CL_FREQ` |
| 항목 그룹 `Group1~4` (`GRP_NAME`) | 차원 `ITEM_CODE1~4`, 코드리스트 `CL_{STAT}_ITEM_CODE{n}` |
| 항목코드·이름·상위항목 | 코드 id·name·parent |
| `TIME` | `TIME_PERIOD` (`2024`, `2024-S1`, `2024-Q1`, `2024-01`, `2024-01-15`) |
| `DATA_VALUE` | 측정값 `OBS_VALUE` (+ 변환 시 `YOY_PCT`/`POP_PCT`) |
| `UNIT_NAME` | 시계열 속성 `UNIT_MEASURE` |

- SDMX id에 쓸 수 없는 문자가 있는 ECOS 코드는 되돌릴 수 있는 방식으로 이스케이프합니다. 예: `*AA` → `$2AAA`. 원래 코드는 `ECOS_ITEM_CODE` 주석에 남습니다.
- `get_metadata(output_format="sdmx")`는 structure message를, `get_data(output_format="sdmx")`는 data message를 반환합니다. 둘 다 [공식 SDMX-JSON 2.1 스키마](https://github.com/sdmx-twg/sdmx-json)로 검증하는 테스트가 있습니다(`uv run pytest -m live`).
- 전체 규칙은 리소스 `ecos://sdmx/conventions`에서 확인할 수 있습니다.

### 응답 포맷과 크기

| `output_format` | 용도 | 크기 (CPI 24개월 단일 계열, 문자 수) |
|---|---|---|
| `compact` (기본값) | LLM 분석. 계열별로 이름·단위를 한 번만 쓰고 값은 `[시점, 값]` | 605 |
| `csv` | 표·차트 작업 | 805 |
| `sdmx` | 다른 시스템 연동, 표준 준수가 필요할 때 | 2,186 |
| `json` | ECOS 원본 행 | 6,545 |

---

## 📈 데이터 조회 기능

- **증감률**:
  - `transform="yoy"`: 전년동기대비 증감률(%)을 계산합니다. 기준 시점 데이터는 따로 조회하므로, 결과 건수는 요청한 기간 기준으로 정확합니다.
  - `transform="pop"`: 직전 관측치 대비 증감률(%)을 계산합니다.
- **변경 시점만**: `changes_only=True`를 주면 값이 바뀐 시점만 반환합니다. 기준금리 프리셋에는 기본으로 적용됩니다.
- **유연한 날짜**: `"2024"`, `"2024-03"`, `"2024Q1"`, `"2024-03-15"`처럼 입력해도 조회 주기에 맞게 변환됩니다. 예를 들어 월간 조회에서 `end_date="2024"`는 `202412`가 됩니다.
- **기본 기간**: 날짜를 생략하면 일간(D)은 최근 3개월, 그 외 주기는 최근 2년입니다. 날짜는 한국 시간 기준으로 계산합니다.
- **최신 구간 우선**: 결과가 `end_count`를 넘으면(sample 키는 10건) 가장 최근 구간을 반환하고 `truncated: true`를 붙입니다. 과거부터 받으려면 `prefer_latest=False`를 쓰세요.

### 주기(Cycle)별 날짜 포맷

| 주기 | 이름 | ECOS 포맷 | 예시 |
|:---:|:---:|:---:|:---:|
| `A` | 연간 | `YYYY` | `2024` |
| `S` | 반기 | `YYYYS1`/`YYYYS2` | `2023S2` |
| `Q` | 분기 | `YYYYQ1`~`YYYYQ4` | `2024Q3` |
| `M` | 월간 | `YYYYMM` | `202412` |
| `SM` | 반월 | `YYYYMMS1`/`YYYYMMS2` | `202401S2` |
| `D` | 일간 | `YYYYMMDD` | `20240315` |

---

## 📌 인기 지표 프리셋 (`get_data(indicator=...)`)

| 지표 | 키워드(별칭) | 통계표 | 주기 | 항목코드 | 기본 처리 |
|------|-------------|:---:|:---:|:---:|:---:|
| 한국은행 기준금리 | `기준금리`, `금리`, `base_rate` | `722Y001` | D | `0101000` | 변경 시점만 |
| 경제성장률(실질, 전기비 %) | `성장률`, `경제성장률`, `GDP성장률` | `200Y102` | Q | `10111` | |
| 실질 GDP(십억원) | `GDP`, `실질GDP`, `국내총생산` | `200Y108` | Q | `10601` | |
| 소비자물가상승률(%) | `물가상승률`, `인플레이션` | `901Y009` | M | `0` | `yoy` |
| 소비자물가지수 | `CPI`, `소비자물가`, `물가` | `901Y009` | M | `0` | |
| 원/달러 환율(일별) | `환율`, `원달러`, `달러`, `USD` | `731Y001` | D | `0000001` | |
| 원/달러 환율(월평균) | `월평균환율` | `731Y004` | M | `0000001`/`0000100` | |
| 본원통화(평잔) | `본원통화` | `102Y004` | M | `ABA1` | |
| M2 광의통화 | `M2`, `통화량`, `광의통화` | `161Y006` | M | `BBHA00` | |
| 국고채 3년(일별) | `국고채`, `국고채3년`, `채권금리` | `817Y002` | D | `010200000` | |
| 국고채 3년(월평균) | `국고채월평균` | `721Y001` | M | `5020000` | |
| 생산자물가지수 | `PPI`, `생산자물가` | `404Y014` | M | `*AA` | |

`"통화"`, `"지수"`처럼 여러 지표에 해당하는 키워드를 넣으면 후보 목록이 담긴 에러가 돌아옵니다.

---

## 🚀 빠른 시작

1. **API 키 발급(선택)**: [ECOS Open API](https://ecos.bok.or.kr/api/#/)에서 무료로 발급받을 수 있습니다. 키가 없으면 `sample` 키가 쓰이며, 1회 조회가 10건으로 제한됩니다.
2. **자가 진단**: API 키, ECOS 서버 연결, 통계표 인덱스를 확인합니다. ([uv](https://docs.astral.sh/uv/)가 필요합니다.)
   ```bash
   ECOS_API_KEY=your_api_key_here uvx ecos-mcp --check
   ```

## 🔧 MCP 클라이언트 설정

### Claude Desktop

설정 파일: macOS `~/Library/Application Support/Claude/claude_desktop_config.json`, Windows `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ecos": {
      "command": "uvx",
      "args": ["ecos-mcp"],
      "env": { "ECOS_API_KEY": "your_api_key_here" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add ecos -e ECOS_API_KEY=your_api_key_here -- uvx ecos-mcp
```

### Cursor

`~/.cursor/mcp.json`에 위와 같은 `mcpServers` 설정을 추가하세요.

### 최신 개발 버전 / 로컬 클론

- GitHub 최신 코드: `"args"`를 `["--from", "git+https://github.com/kgy0617/ecos_mcp", "ecos-mcp"]`로 바꿉니다.
- 로컬 클론: `"command": "uv"`, `"args": ["--directory", "/path/to/ecos_mcp", "run", "ecos-mcp"]`로 설정합니다. 클론 폴더의 `.env`에 키를 넣어도 됩니다.

---

## 📖 사용 예시

아래 응답은 실제 호출 결과를 줄인 것입니다(`…`로 생략 표시).

### 1. 품목 단위 및 의미 검색: `search_statistics`
> "쌀 가격이나 물가(inflation) 관련 통계 찾아줘"

```json
{"query": "쌀"}
```
```json
{
  "item_matches": [
    {
      "item_name": "쌀",
      "category": "소비자물가 세부품목",
      "stat_code": "901Y009",
      "stat_name": "소비자물가지수(2020=100)",
      "cycle": "M",
      "item_code1": "01111",
      "get_data_example": {"stat_code": "901Y009", "cycle": "M", "item_code1": "01111"}
    }
  ]
}
```
`"inflation"`이나 `"기준금리"`로 검색하면 SDMX 표준 개념(`CPI_HEADLINE`, `BOK_BASE_RATE`)과 매핑된 통계표를 자동으로 탐색합니다.

### 2. 표준 개념 및 출처 근거(Evidence) 포함 조회: `get_data`
> "소비자물가지수 최근 3개월 조회하고 출처 근거 알려줘"

```json
{"indicator": "CPI_HEADLINE", "start_date": "202401", "end_date": "202403"}
```
```json
{
  "stat_code": "901Y009",
  "stat_name": "4.2.1. 소비자물가지수",
  "indicator": "CPI_HEADLINE",
  "cycle": "M",
  "series": [{"item": "총지수", "item_code": "0", "unit": "2020=100",
    "data": [["202401", 113.15], ["202402", 113.77], ["202403", 113.94]]}],
  "evidence": {
    "source_agency": "한국은행 (Bank of Korea)",
    "system": "경제통계시스템 (ECOS - Economic Statistics System)",
    "table_code": "901Y009",
    "table_name": "4.2.1. 소비자물가지수",
    "series_key": "M.901Y009.0",
    "period": "202401 ~ 202403",
    "unit": "2020=100",
    "retrieved_at": "2026-09-26T10:15:00+09:00",
    "ecos_url": "https://ecos.bok.or.kr/#/Search/901Y009",
    "citation": "출처: 한국은행 경제통계시스템(ECOS), '4.2.1. 소비자물가지수' (901Y009, 계열키: M.901Y009.0), 조회 시각: 2026-09-26 10:15 KST",
    "sdmx_concept": "CPI_HEADLINE"
  }
}
```
CSV 출력 시 상단 헤더에 `# evidence_source_agency`, `# citation` 주석으로 첨부되어 모든 LLM이 응답 작성 시 정확한 출처를 인용할 수 있습니다.

### 3. 통계표 구조와 SDMX 개념 매핑 확인: `get_metadata`
> "주요국 환율 통계표에는 어떤 항목과 SDMX 개념이 연결되어 있어?"

```json
{"stat_code": "731Y004", "codes_limit": 2}
```
```json
{"dataflow":{"id":"731Y004","name":"3.1.2.1. 주요국 통화의 대원화환율",
   "urn":"urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=ECOS_MCP:731Y004(1.0)"},
 "series_key":["FREQ","ITEM_CODE1","ITEM_CODE2"],
 "sdmx_concepts": [
   {"concept_id": "EXR_USD_KRW", "name": "원/달러 환율", "ref_area": "KR", "imf_code": "ENDA_XDC_USD_RATE"}
 ],
 "dimensions":[
   {"id":"FREQ","name":"주기","codes":[{"code":"M","name":"월간"},{"code":"Q","name":"분기"}]},
   {"id":"ITEM_CODE1","name":"계정항목","total_codes":24,"codes":[
     {"code":"0000001","name":"원/미국달러(매매기준율)","unit":"원"}]}],
 "get_data_example":{"stat_code":"731Y004","cycle":"M","item_code1":"0000001","item_code2":"0000100"}}
```
`output_format="sdmx"`를 주면 공식 SDMX-JSON structure message(Dataflow·DSD·Codelist·ConceptScheme)로 반환됩니다.

### 4. 데이터 정규화 및 재지수화 비교: `compare_series`
> "환율과 물가지수를 2024년 1월=100 기준으로 재지수화해서 비교해줘"

```json
{
  "series": [
    {"stat_code": "901Y009", "cycle": "M", "item_code1": "0", "label": "CPI"},
    {"stat_code": "731Y004", "cycle": "M", "item_code1": "0000001", "label": "USD_KRW"}
  ],
  "start_date": "202401",
  "end_date": "202403",
  "normalize_method": "rebase",
  "rebase_period": "202401"
}
```
서로 단위가 다르거나(원 vs 지수), 기준연도가 다른 지표들을 동일 시점 100 기준으로 재정렬하여 거시 시계열 간의 상대적 상승 추세를 한눈에 비교할 수 있습니다.

### 5. 두 지표의 상관관계 비교: `compare_series`
> "원/달러 환율과 국고채 금리가 같이 움직였어?"

```json
{"series": [{"indicator": "월평균환율", "label": "원/달러"},
            {"indicator": "국고채월평균", "label": "국고채3년"}],
 "start_date": "2025-11", "end_date": "2026-08"}
```
```json
{"frequency":"M","aggregation":"mean",
 "correlation":{"원/달러 ~ 국고채3년":{"r":0.3005,"n":10}},
 "columns":["time","원/달러","국고채3년"],
 "rows":[["202511",1457.77,2.88],["202512",1467.4,3.01],…,["202608",1406.3,3.788]]}
```
주기가 다른 계열(예: 일별 기준금리와 월별 물가)은 가장 낮은 빈도로 맞춰 집계합니다.

### 6. 요약통계: `calculate_statistics`
```json
{"indicator": "월평균환율", "start_date": "2025-11"}
```
```json
{"series":[{"item":"원/미국달러(매매기준율) / 평균자료","unit":"원","stats":{
  "count":10,"min":{"time":"202608","value":1406.3},"max":{"time":"202606","value":1527.3},
  "mean":1472.617,"change_pct":-3.53,"trend_per_year":16.2945,"trend_r2":0.016,
  "pop_pct_std":2.593,"max_drawdown_pct":-7.92,"latest_yoy_pct":1.2,…}}]}
```

### 7. 지표 설명: `explain_indicator`
```json
{"term": "경제심리지수"}
```
```json
{"term":"경제심리지수",
 "definition":[{"word":"경제심리지수(ESI)","definition":"기업과 소비자 모두를 포함한 민간의 경제상황에 대한 심리를 …"}],
 "methodology":[{"dataset":"경제심리지수"},{"section":"담당기관","text":"한국은행"},…],
 "tables":[{"STAT_CODE":"513Y001","STAT_NAME":"6.3. 경제심리지수","CYCLE":"M"}],
 "sdmx":{"dataflow":{"id":"513Y001",…},"series_key":["FREQ","ITEM_CODE1"],…}}
```

---

## 📚 Resources & Prompts

- **Resources**
  - `ecos://popular-indicators`: 인기 지표 프리셋
  - `ecos://date-format-guide`: 주기별 날짜 규격
  - `ecos://sdmx/conventions`: ECOS→SDMX 매핑 규칙
- **Prompts**
  - `macro-economic-briefing`: 100대 지표, 요약통계, 금리·물가 비교를 묶은 거시경제 브리핑
  - `analyze-economic-trend`: 설명 → 조회 → 통계 순서로 진행하는 지표 추이 분석

---

## 🧪 개발 & 테스트

```bash
git clone https://github.com/kgy0617/ecos_mcp.git
cd ecos_mcp
uv sync

uv run pytest            # 오프라인 테스트 (ECOS API 모킹, 네트워크 불필요)
uv run pytest -m live    # 실제 ECOS API + 공식 SDMX-JSON 스키마 검증
```

### 통계표 인덱스 갱신

`search_statistics`는 패키지에 포함된 `tables.json`(생성일 기록)을 사용합니다. ECOS 통계표 목록이 바뀌면 다시 생성하세요.

```bash
ECOS_API_KEY=your_api_key_here uv run python scripts/update_tables.py
```

---

## 📝 라이선스

MIT License
