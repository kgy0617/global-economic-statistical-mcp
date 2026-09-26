"""Concept Catalog: country-agnostic economic concepts mapped to provider series.

A concept (e.g. ``CPI_YOY``) says *what* is measured and in which canonical unit.
Each :class:`SourceMapping` says *where* to get it: provider, dataflow, key template and
the unit that source publishes. Key templates use ``{ISO2}``, ``{ISO3}`` and ``{CUR}``.

Every mapping below was checked against the live provider API for the default economies
(KR, US, JP, CN, EA, GB; tests/test_live.py re-verifies them). For Korea the national
source (ECOS) comes first; international sources follow in priority order and are used for
other economies and for cross-validation. ``excludes`` records economies a provider is known
not to publish, so the resolver says so instead of returning an empty series.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from global_economic_statistical_mcp.catalog.countries import Country

OECD_PRICES = "OECD.SDD.TPS:DSD_PRICES@DF_PRICES_ALL(1.0)"
OECD_FINMARK = "OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)"
OECD_QNA_GROWTH = "OECD.SDD.NAD:DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD(1.1)"
OECD_UNEMP = "OECD.SDD.TPS:DSD_LFS@DF_IALFS_UNE_M(1.0)"
OECD_CLI = "OECD.SDD.STES:DSD_STES@DF_CLI(4.1)"
IMF_CPI = "IMF.STA:CPI"
IMF_ER = "IMF.STA:ER"
IMF_BOP = "IMF.STA:BOP"
IMF_IRFCL = "IMF.STA:IRFCL"
IMF_QNEA = "IMF.STA:QNEA"
IMF_PPI = "IMF.STA:PPI"
BIS_CBPOL = "BIS:WS_CBPOL(1.0)"
BIS_XRU = "BIS:WS_XRU(1.0)"
BIS_SPP = "BIS:WS_SPP(1.0)"
BIS_CPI = "BIS:WS_LONG_CPI(1.0)"


@dataclass(frozen=True)
class SourceMapping:
    provider: str  # ECOS | OECD | IMF | BIS
    dataflow: str  # ECOS stat code, or SDMX "AGENCY:ID(VERSION)"
    key: str  # ECOS: dot-joined item codes; SDMX: key template
    freq: str
    unit: str  # canonical unit this source publishes
    unit_mult: int = 0
    base_period: str | None = None
    adjustment: str | None = None  # "SA" | "NSA"
    countries: tuple[str, ...] | None = None  # None: any country the provider covers
    excludes: tuple[str, ...] = ()  # economies the provider does not publish
    area_codes: tuple[tuple[str, str], ...] = ()  # (country, code) where this dataflow differs from the provider default
    transform: str | None = None  # computed by this server (e.g. ECOS CPI → yoy)
    changes_only: bool = False
    note: str | None = None

    def covers(self, country: Country) -> bool:
        return (self.countries is None or country.iso2 in self.countries) and country.iso2 not in self.excludes

    def render_key(self, country: Country) -> str:
        area = dict(self.area_codes).get(country.iso2) or country.code_for(self.provider)
        return self.key.format(ISO2=area or country.iso2, ISO3=area or country.iso3, CUR=country.currency)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, False, 0, ()) or k in ("unit_mult",)}


@dataclass(frozen=True)
class Concept:
    id: str
    name_ko: str
    name_en: str
    category: str
    unit: str  # canonical unit of the concept
    description_ko: str
    description_en: str
    synonyms: tuple[str, ...]
    sources: tuple[SourceMapping, ...]
    aliases: tuple[str, ...] = ()  # former preset ids kept for compatibility
    notes: tuple[str, ...] = field(default_factory=tuple)
    # How to aggregate to a lower frequency: flows are summed, stocks take the end-of-period
    # value, rates, prices and indices are averaged.
    aggregation: str = "mean"

    def sources_for(self, country: Country, provider: str | None = None, freq: str | None = None) -> list[SourceMapping]:
        return [
            s
            for s in self.sources
            if s.covers(country)
            and (provider is None or s.provider == provider.upper())
            and (freq is None or s.freq == freq.upper())
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "concept_id": self.id,
            "name_ko": self.name_ko,
            "name_en": self.name_en,
            "category": self.category,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "providers": sorted({s.provider for s in self.sources}),
            "korea_only": all(s.countries == ("KR",) for s in self.sources),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "description_ko": self.description_ko,
            "description_en": self.description_en,
            "synonyms": list(self.synonyms),
            "sources": [s.to_dict() for s in self.sources],
            **({"notes": list(self.notes)} if self.notes else {}),
        }


KR = ("KR",)
NO_USD = ("US",)  # national currency per US dollar is meaningless for the United States


def _ecos(dataflow: str, key: str, freq: str, unit: str, **kw: Any) -> SourceMapping:
    return SourceMapping("ECOS", dataflow, key, freq, unit, countries=KR, **kw)


CONCEPTS: tuple[Concept, ...] = (
    # ── Interest rates ──────────────────────────────────────────────
    Concept(
        id="POLICY_RATE",
        name_ko="정책금리(기준금리)",
        name_en="Central bank policy rate",
        category="interest_rate",
        unit="PC_PA",
        description_ko="중앙은행이 통화정책 운용 목표로 정하는 정책금리입니다. 한국은 한국은행 기준금리이고, 다른 나라는 BIS가 대표 정책금리로 수록한 금리입니다. 유로지역 회원국은 유로지역(EA) 금리를 쓰세요.",
        description_en="The policy rate set by the central bank (for Korea the Bank of Korea Base Rate; elsewhere the rate BIS lists as the policy rate). Euro-area members use the euro-area (EA) rate.",
        synonyms=("기준금리", "정책금리", "금리", "policy rate", "base rate", "central bank rate", "bok rate", "fed funds", "ecb rate", "bank rate"),
        aliases=("base_rate", "BOK_BASE_RATE"),
        sources=(
            _ecos("722Y001", "0101000", "D", "PC_PA", changes_only=True, note="한국은행 기준금리(일별, 변경 시점만)"),
            _ecos("722Y001", "0101000", "M", "PC_PA", note="한국은행 기준금리(월)"),
            SourceMapping("BIS", BIS_CBPOL, "M.{ISO2}", "M", "PC_PA", note="BIS central bank policy rates (monthly)"),
            SourceMapping("BIS", BIS_CBPOL, "D.{ISO2}", "D", "PC_PA", note="BIS central bank policy rates (daily)"),
        ),
    ),
    Concept(
        id="LONG_TERM_RATE",
        name_ko="장기금리(국채 10년)",
        name_en="Long-term interest rate (10-year government bond)",
        category="interest_rate",
        unit="PC_PA",
        description_ko="10년 만기 국채 수익률(월평균)입니다. OECD 장기금리(IRLT)는 대체로 10년물 국채를 기준으로 합니다.",
        description_en="10-year government bond yield, monthly average (OECD IRLT is generally the 10-year benchmark).",
        synonyms=("장기금리", "국채10년", "국고채10년", "10년물", "long term rate", "10y yield", "bond yield 10y"),
        aliases=("BOND_KTB_10Y",),
        sources=(
            _ecos("721Y001", "5050000", "M", "PC_PA", note="국고채(10년) 월평균"),
            SourceMapping("OECD", OECD_FINMARK, "{ISO3}.M.IRLT.PA._Z._Z._Z._Z.N", "M", "PC_PA"),
        ),
    ),
    Concept(
        id="SHORT_TERM_RATE",
        name_ko="단기금리(3개월)",
        name_en="Short-term interest rate (3-month)",
        category="interest_rate",
        unit="PC_PA",
        description_ko="3개월 만기 단기시장금리(월평균)입니다. 한국은 CD(91일) 금리가 대표 지표입니다.",
        description_en="3-month money market rate, monthly average; for Korea the 91-day CD rate.",
        synonyms=("단기금리", "CD금리", "CD91일", "3개월금리", "short term rate", "3m rate", "interbank rate"),
        sources=(
            _ecos("721Y001", "2010000", "M", "PC_PA", note="CD(91일) 월평균"),
            SourceMapping("OECD", OECD_FINMARK, "{ISO3}.M.IR3TIB.PA._Z._Z._Z._Z.N", "M", "PC_PA"),
        ),
    ),
    Concept(
        id="KTB_3Y",
        name_ko="국고채 3년 수익률",
        name_en="Korea Treasury Bond 3-year yield",
        category="interest_rate",
        unit="PC_PA",
        description_ko="국내 채권시장의 대표 지표금리인 3년 만기 국고채 수익률입니다.",
        description_en="Yield on 3-year Korea Treasury Bonds, the benchmark of the Korean bond market.",
        synonyms=("국고채", "국고채3년", "채권금리", "시장금리", "ktb3y", "treasury 3y", "국고채월평균"),
        aliases=("treasury_3y", "treasury_3y_monthly", "BOND_KTB_3Y_DAILY", "BOND_KTB_3Y_MONTHLY"),
        sources=(
            _ecos("817Y002", "010200000", "D", "PC_PA", note="일별"),
            _ecos("721Y001", "5020000", "M", "PC_PA", note="월평균"),
        ),
    ),
    # ── Prices ──────────────────────────────────────────────────────
    Concept(
        id="CPI",
        name_ko="소비자물가지수",
        name_en="Consumer price index (all items)",
        category="price",
        unit="IX",
        description_ko="가계가 구입하는 상품과 서비스의 가격 변동을 종합한 지수(총지수)입니다. 기준연도가 기관·국가마다 다릅니다(ECOS 2020, BIS 2010, OECD 2015, IMF는 국가별: 한국 2020·미국 2010). 유로지역은 HICP입니다.",
        description_en="Headline consumer price index (HICP for the euro area). Base years differ by source and country (ECOS 2020, BIS 2010, OECD 2015, IMF varies: KR 2020, US 2010).",
        synonyms=("소비자물가지수", "소비자물가", "물가지수", "물가", "cpi", "consumer price index"),
        aliases=("CPI_HEADLINE",),
        sources=(
            _ecos("901Y009", "0", "M", "IX", base_period="2020"),
            SourceMapping(
                "IMF", IMF_CPI, "{ISO3}.CPI._T.IX.M", "M", "IX", excludes=("EA",), note="기준연도는 국가마다 다름(한국 2020, 미국 2010)"
            ),
            SourceMapping("BIS", BIS_CPI, "M.{ISO2}.628", "M", "IX", base_period="2010"),
            # OECD publishes no CPI for Japan, and its euro-area series stopped at the 2026 enlargement.
            SourceMapping("OECD", OECD_PRICES, "{ISO3}.M.N.CPI.IX._T.N._Z", "M", "IX", base_period="2015", excludes=("JP", "EA")),
        ),
    ),
    Concept(
        id="CPI_YOY",
        name_ko="소비자물가상승률(전년동월비)",
        name_en="CPI inflation (year on year)",
        category="price",
        unit="PC_YOY",
        description_ko="소비자물가지수의 전년 동월 대비 변화율(%)입니다. ECOS는 지수에서 이 서버가 계산하고, IMF·BIS·OECD는 기관 발표값입니다.",
        description_en="Year-on-year change in the CPI (%). Computed by this server for ECOS; published by IMF, BIS and OECD.",
        synonyms=("물가상승률", "소비자물가상승률", "인플레이션", "inflation", "cpi inflation", "cpi yoy"),
        aliases=("inflation", "CPI_INFLATION_RATE"),
        sources=(
            _ecos("901Y009", "0", "M", "PC_YOY", transform="yoy"),
            SourceMapping("IMF", IMF_CPI, "{ISO3}.CPI._T.YOY_PCH_PA_PT.M", "M", "PC_YOY", excludes=("EA",)),
            SourceMapping("BIS", BIS_CPI, "M.{ISO2}.771", "M", "PC_YOY"),
            SourceMapping("OECD", OECD_PRICES, "{ISO3}.M.N.CPI.PA._T.N.GY", "M", "PC_YOY", excludes=("JP", "EA")),
        ),
    ),
    Concept(
        id="PPI",
        name_ko="생산자물가지수",
        name_en="Producer price index (all items)",
        category="price",
        unit="IX",
        description_ko="국내 생산자가 공급하는 상품과 서비스의 가격 변동을 종합한 지수(총지수)입니다. 국제 출처(IMF)는 일부 국가만 제공합니다(기본 검증 대상 중 미국).",
        description_en="Headline producer price index. The international source (IMF) covers only some economies (of the default set, the US).",
        synonyms=("생산자물가지수", "생산자물가", "ppi", "producer price index"),
        aliases=("PPI_HEADLINE",),
        sources=(
            _ecos("404Y014", "*AA", "M", "IX", base_period="2020"),
            SourceMapping("IMF", IMF_PPI, "{ISO3}.PPI.IX.M", "M", "IX", excludes=("KR", "JP", "CN", "EA", "GB")),
        ),
    ),
    # ── National accounts ───────────────────────────────────────────
    Concept(
        id="GDP_REAL_GROWTH_QOQ",
        name_ko="실질 경제성장률(전기비, 계절조정)",
        name_en="Real GDP growth (quarter on quarter, seasonally adjusted)",
        category="national_accounts",
        unit="PC_POP",
        description_ko="계절조정 실질 GDP의 전분기 대비 성장률(%)입니다.",
        description_en="Quarter-on-quarter growth of seasonally adjusted real GDP (%).",
        synonyms=("경제성장률", "성장률", "gdp성장률", "실질성장률", "gdp growth", "economic growth", "qoq growth"),
        aliases=("gdp_growth", "GDP_REAL_GROWTH"),
        sources=(
            _ecos("200Y102", "10111", "Q", "PC_POP", adjustment="SA"),
            SourceMapping(
                "OECD", OECD_QNA_GROWTH, "Q.Y.{ISO3}.S1.S1.B1GQ._Z._Z._Z.PC.L.G1.T0102", "Q", "PC_POP",
                adjustment="SA", area_codes=(("EA", "EA"),),
            ),
        ),
    ),
    Concept(
        id="GDP_REAL_GROWTH_YOY",
        name_ko="실질 경제성장률(전년동기비)",
        name_en="Real GDP growth (year on year)",
        category="national_accounts",
        unit="PC_YOY",
        description_ko="실질 GDP의 전년 동기 대비 성장률(%)입니다. ECOS는 원계열, OECD는 계절조정 계열 기준이라 소폭 다를 수 있습니다.",
        description_en="Year-on-year real GDP growth. ECOS uses the original series, OECD the seasonally adjusted one.",
        synonyms=("전년동기비성장률", "연간성장률", "gdp yoy", "yoy growth"),
        sources=(
            _ecos("200Y102", "10211", "Q", "PC_YOY", adjustment="NSA"),
            SourceMapping(
                "OECD", OECD_QNA_GROWTH, "Q.Y.{ISO3}.S1.S1.B1GQ._Z._Z._Z.PC.L.GY.T0102", "Q", "PC_YOY",
                adjustment="SA", area_codes=(("EA", "EA"),),
            ),
        ),
    ),
    Concept(
        id="GDP_REAL",
        name_ko="실질 국내총생산(GDP)",
        name_en="Real gross domestic product",
        category="national_accounts",
        unit="XDC",
        description_ko="계절조정 실질 GDP(분기, 자국통화)입니다. ECOS는 십억원, IMF는 자국통화 단위이며 중국은 원계열만 있습니다.",
        description_en="Seasonally adjusted real GDP, quarterly, national currency (ECOS in billions of KRW; IMF in units; China is not seasonally adjusted).",
        synonyms=("실질gdp", "국내총생산", "gdp", "real gdp"),
        aliases=("gdp",),
        aggregation="sum",
        sources=(
            _ecos("200Y108", "10601", "Q", "XDC", unit_mult=9, adjustment="SA"),
            SourceMapping("IMF", IMF_QNEA, "{ISO3}.B1GQ.Q.SA.XDC.Q", "Q", "XDC", adjustment="SA", excludes=("CN",)),
            SourceMapping("IMF", IMF_QNEA, "{ISO3}.B1GQ.Q.NSA.XDC.Q", "Q", "XDC", adjustment="NSA", countries=("CN",)),
        ),
    ),
    Concept(
        id="GDP_NOMINAL",
        name_ko="명목 국내총생산(GDP)",
        name_en="Nominal gross domestic product",
        category="national_accounts",
        unit="XDC",
        description_ko="계절조정 명목 GDP(분기, 자국통화)입니다. ECOS는 십억원, IMF는 자국통화 단위이며 중국은 원계열만 있습니다.",
        description_en="Seasonally adjusted nominal GDP, quarterly, national currency (ECOS in billions of KRW; IMF in units; China is not seasonally adjusted).",
        synonyms=("명목gdp", "nominal gdp"),
        aggregation="sum",
        sources=(
            _ecos("200Y107", "10601", "Q", "XDC", unit_mult=9, adjustment="SA"),
            SourceMapping("IMF", IMF_QNEA, "{ISO3}.B1GQ.V.SA.XDC.Q", "Q", "XDC", adjustment="SA", excludes=("CN",)),
            SourceMapping("IMF", IMF_QNEA, "{ISO3}.B1GQ.V.NSA.XDC.Q", "Q", "XDC", adjustment="NSA", countries=("CN",)),
        ),
    ),
    # ── Labour ──────────────────────────────────────────────────────
    Concept(
        id="UNEMPLOYMENT_RATE",
        name_ko="실업률(원계열)",
        name_en="Unemployment rate (not seasonally adjusted)",
        category="labor",
        unit="PC",
        description_ko="경제활동인구 중 실업자 비율(%)입니다. 계절조정하지 않은 원계열 기준입니다.",
        description_en="Unemployed as a share of the labour force (%), not seasonally adjusted.",
        synonyms=("실업률", "unemployment", "unemployment rate", "jobless rate"),
        aliases=("LABOR_UNEMPLOYMENT_RATE",),
        sources=(
            _ecos("901Y027", "I61BC.I28A", "M", "PC", adjustment="NSA", note="실업률 원계열"),
            SourceMapping(
                "OECD", OECD_UNEMP, "{ISO3}.UNE_LF_M.PT_LF_SUB._Z.N._T.Y_GE15._Z.M", "M", "PC", adjustment="NSA",
                excludes=("CN",), area_codes=(("EA", "EA"),),
            ),
        ),
    ),
    Concept(
        id="UNEMPLOYMENT_RATE_SA",
        name_ko="실업률(계절조정)",
        name_en="Unemployment rate (seasonally adjusted)",
        category="labor",
        unit="PC",
        description_ko="계절조정 실업률(%)입니다.",
        description_en="Seasonally adjusted unemployment rate (%).",
        synonyms=("계절조정실업률", "sa unemployment", "seasonally adjusted unemployment"),
        sources=(
            _ecos("901Y027", "I61BC.I28B", "M", "PC", adjustment="SA", note="실업률 계절조정"),
            SourceMapping(
                "OECD", OECD_UNEMP, "{ISO3}.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M", "M", "PC", adjustment="SA",
                excludes=("CN",), area_codes=(("EA", "EA"),),
            ),
        ),
    ),
    # ── Exchange rates ──────────────────────────────────────────────
    Concept(
        id="USD_EXCHANGE_RATE",
        name_ko="대미달러 환율(월평균)",
        name_en="Exchange rate, national currency per US dollar (monthly average)",
        category="exchange_rate",
        unit="XDC_USD",
        description_ko="미국 달러 1단위당 자국 통화 환율의 월평균입니다. 한국은 원/달러 매매기준율 월평균, 유로지역은 달러당 유로입니다. 미국에는 해당하지 않습니다.",
        description_en="National currency per US dollar, period average (KRW/USD basic rate for Korea, EUR per USD for the euro area). Not applicable to the United States.",
        synonyms=("환율", "원달러", "원/달러", "월평균환율", "달러환율", "exchange rate", "usd exchange rate", "fx rate"),
        aliases=("usd_krw_monthly", "EXR_USD_KRW_MONTHLY"),
        sources=(
            _ecos("731Y004", "0000001.0000100", "M", "XDC_USD"),
            SourceMapping("IMF", IMF_ER, "{ISO3}.XDC_USD.PA_RT.M", "M", "XDC_USD", excludes=NO_USD),
            SourceMapping("BIS", BIS_XRU, "M.{ISO2}.{CUR}.A", "M", "XDC_USD", excludes=NO_USD),
            SourceMapping("OECD", OECD_FINMARK, "{ISO3}.M.CC.XDC_USD._Z._Z._Z._Z.N", "M", "XDC_USD", excludes=NO_USD),
        ),
    ),
    Concept(
        id="USD_EXCHANGE_RATE_DAILY",
        name_ko="대미달러 환율(일별)",
        name_en="Exchange rate, national currency per US dollar (daily)",
        category="exchange_rate",
        unit="XDC_USD",
        description_ko="미국 달러 1단위당 자국 통화 환율(일별)입니다. 한국은 서울외환시장 원/달러 매매기준율입니다.",
        description_en="Daily national currency per US dollar; for Korea the Seoul market KRW/USD basic rate.",
        synonyms=("일별환율", "원달러일별", "usdkrw", "usd krw daily", "달러", "daily exchange rate"),
        aliases=("usd_krw", "EXR_USD_KRW_DAILY"),
        sources=(
            _ecos("731Y001", "0000001", "D", "XDC_USD"),
            SourceMapping("BIS", BIS_XRU, "D.{ISO2}.{CUR}.A", "D", "XDC_USD", excludes=NO_USD),
        ),
    ),
    # ── Money, external, markets, sentiment, housing ────────────────
    Concept(
        id="M2",
        name_ko="광의통화(M2, 평잔)",
        name_en="Broad money M2 (period average)",
        category="money",
        unit="XDC",
        description_ko="한국의 M2 광의통화(평잔, 원계열, 십억원)입니다. 광의통화 정의가 국가마다 달라 한국만 제공합니다.",
        description_en="Korea's broad money M2, period average, original series, billions of KRW. Korea only: broad-money definitions differ by country.",
        synonyms=("m2", "광의통화", "통화량", "broad money", "money supply"),
        aliases=("MONEY_M2",),
        sources=(_ecos("161Y006", "BBHA00", "M", "XDC", unit_mult=9, adjustment="NSA"),),
    ),
    Concept(
        id="MONETARY_BASE",
        name_ko="본원통화(평잔, 계절조정)",
        name_en="Monetary base (period average, SA)",
        category="money",
        unit="XDC",
        description_ko="화폐발행액과 지급준비예치금의 합계(평잔, 계절조정, 십억원)입니다.",
        description_en="Currency issued plus bank reserves at the Bank of Korea, SA, billions of KRW.",
        synonyms=("본원통화", "monetary base", "reserve money", "base money"),
        aliases=("reserve_money", "MONEY_BASE"),
        sources=(_ecos("102Y004", "ABA1", "M", "XDC", unit_mult=9, adjustment="SA"),),
    ),
    Concept(
        id="CURRENT_ACCOUNT",
        name_ko="경상수지",
        name_en="Current account balance",
        category="external",
        unit="USD",
        description_ko="상품·서비스·본원소득·이전소득수지의 합계(미국달러)입니다. ECOS는 월별 백만달러, IMF는 분기별 달러 단위입니다.",
        description_en="Current account balance in US dollars (ECOS monthly, millions; IMF quarterly, units).",
        synonyms=("경상수지", "국제수지", "current account", "balance of payments"),
        aliases=("BOP_CURRENT_ACCOUNT",),
        aggregation="sum",
        sources=(
            _ecos("301Y013", "000000", "M", "USD", unit_mult=6),
            SourceMapping("IMF", IMF_BOP, "{ISO3}.NETCD_T.CAB.USD.Q", "Q", "USD"),
        ),
    ),
    Concept(
        id="GOODS_BALANCE",
        name_ko="상품수지",
        name_en="Goods balance",
        category="external",
        unit="USD",
        description_ko="국제수지 기준 상품수출과 수입의 차이(미국달러)입니다. ECOS는 월별 백만달러, IMF는 분기별 달러 단위입니다.",
        description_en="Goods balance on a balance-of-payments basis in US dollars (ECOS monthly, millions; IMF quarterly, units).",
        synonyms=("상품수지", "무역수지", "trade balance", "goods balance"),
        aliases=("BOP_GOODS_BALANCE",),
        aggregation="sum",
        sources=(
            _ecos("301Y013", "100000", "M", "USD", unit_mult=6),
            SourceMapping("IMF", IMF_BOP, "{ISO3}.NETCD_T.G.USD.Q", "Q", "USD"),
        ),
    ),
    Concept(
        id="FX_RESERVES",
        name_ko="외환보유액",
        name_en="Official foreign exchange reserves",
        category="external",
        unit="USD",
        description_ko="통화당국과 정부가 보유한 공식 준비자산(외환보유액) 합계(미국달러)입니다. ECOS는 천달러, IMF(IRFCL)는 달러 단위입니다.",
        description_en="Official reserve assets held by the monetary authorities and central government, US dollars (ECOS in thousands; IMF IRFCL in units).",
        synonyms=("외환보유액", "외환보유고", "fx reserves", "foreign reserves", "international reserves"),
        aliases=("FOREIGN_RESERVES",),
        aggregation="last",
        sources=(
            _ecos("732Y001", "99", "M", "USD", unit_mult=3),
            SourceMapping("IMF", IMF_IRFCL, "{ISO3}.IRFCLDT1_IRFCL65_USD.S1XS1311.M", "M", "USD"),
        ),
    ),
    Concept(
        id="SHARE_PRICE_INDEX",
        name_ko="주가지수",
        name_en="Share price index",
        category="market",
        unit="IX",
        description_ko="대표 주가지수입니다. 한국은 KOSPI(1980.1.4=100, 일별), OECD는 월평균 지수(2015=100)라 기준이 다릅니다.",
        description_en="Headline share price index. KOSPI (1980-01-04=100, daily) vs OECD monthly index (2015=100).",
        synonyms=("주가지수", "주가", "코스피", "kospi", "share prices", "stock index"),
        aliases=("STOCK_KOSPI",),
        sources=(
            _ecos("802Y001", "0001000", "D", "IX", base_period="19800104"),
            SourceMapping("OECD", OECD_FINMARK, "{ISO3}.M.SHARE.IX._Z._Z._Z._Z.N", "M", "IX", base_period="2015"),
        ),
    ),
    Concept(
        id="CONSUMER_SENTIMENT",
        name_ko="소비자심리지수(소비자신뢰지수)",
        name_en="Consumer confidence / sentiment index",
        category="sentiment",
        unit="IX",
        description_ko="소비자의 경제 인식을 종합한 지수로, 100보다 크면 장기평균보다 낙관적임을 뜻합니다. 한국은 한국은행 CCSI, 국제 비교는 OECD 소비자신뢰지수(진폭조정, 장기평균=100)입니다.",
        description_en="Consumer confidence; above 100 means more optimistic than the long-run average. Korea: Bank of Korea CCSI; international: OECD amplitude-adjusted consumer confidence (long-run average = 100).",
        synonyms=("소비자심리지수", "소비자심리", "소비심리", "소비자신뢰지수", "ccsi", "consumer sentiment", "consumer confidence"),
        aliases=("SENTIMENT_CCSI",),
        sources=(
            _ecos("511Y002", "FME", "M", "IX"),
            SourceMapping("OECD", OECD_CLI, "{ISO3}.M.CCICP.IX._Z.AA.IX._Z.H", "M", "IX"),
        ),
    ),
    Concept(
        id="BUSINESS_CONFIDENCE",
        name_ko="기업신뢰지수",
        name_en="Business confidence index",
        category="sentiment",
        unit="IX",
        description_ko="제조업 기업의 경기 판단을 종합한 OECD 기업신뢰지수(진폭조정, 장기평균=100)입니다. 100보다 크면 장기평균보다 낙관적입니다.",
        description_en="OECD composite business confidence (manufacturing), amplitude adjusted, long-run average = 100.",
        synonyms=("기업신뢰지수", "business confidence", "business sentiment"),
        sources=(SourceMapping("OECD", OECD_CLI, "{ISO3}.M.BCICP.IX._Z.AA.IX._Z.H", "M", "IX"),),
    ),
    Concept(
        id="ECONOMIC_SENTIMENT",
        name_ko="경제심리지수(ESI, 원계열)",
        name_en="Economic sentiment index (original series)",
        category="sentiment",
        unit="IX",
        description_ko="기업경기실사지수(BSI)와 소비자동향지수(CSI)를 합성한 민간 경제심리 지표입니다.",
        description_en="Composite of business and consumer survey indicators.",
        synonyms=("경제심리지수", "경제심리", "esi", "economic sentiment"),
        aliases=("SENTIMENT_ESI",),
        sources=(_ecos("513Y001", "E1000", "M", "IX"),),
    ),
    Concept(
        id="HOUSE_PRICE_INDEX",
        name_ko="주택매매가격지수",
        name_en="Residential property price index",
        category="real_estate",
        unit="IX",
        description_ko="주택 매매가격 지수입니다. ECOS는 KB 총지수(월, 2026.01=100), BIS는 명목 주거용 부동산가격(분기, 2010=100)입니다.",
        description_en="House price index. ECOS: KB index (monthly, 2026-01=100); BIS: nominal residential prices (quarterly, 2010=100).",
        synonyms=("주택가격", "집값", "주택매매가격", "부동산가격", "house prices", "housing prices", "property prices"),
        aliases=("HOUSING_PRICE_INDEX",),
        sources=(
            _ecos("901Y062", "P63A", "M", "IX", base_period="202601"),
            SourceMapping("BIS", BIS_SPP, "Q.{ISO2}.N.628", "Q", "IX", base_period="2010"),
        ),
    ),
    Concept(
        id="JEONSE_PRICE_INDEX",
        name_ko="주택전세가격지수",
        name_en="Jeonse (lump-sum deposit rent) price index",
        category="real_estate",
        unit="IX",
        description_ko="KB 주택전세가격 총지수(월, 2026.01=100)입니다.",
        description_en="KB jeonse price index (monthly, 2026-01=100).",
        synonyms=("전세", "전세가격", "jeonse"),
        sources=(_ecos("901Y063", "P64A", "M", "IX", base_period="202601"),),
    ),
)

@dataclass(frozen=True)
class KnownDifference:
    """An investigated, documented reason why a provider differs from others for a concept."""

    concept_id: str
    country: str
    provider: str
    explanation: str
    evidence: str  # where the explanation was confirmed (methodology note, release, ...)


# Cross-validation marks a mismatch DIFFER only when a verifiable cause explains it, and
# UNRESOLVED otherwise. Add an entry here only with evidence; never to silence a mismatch.
KNOWN_DIFFERENCES: tuple[KnownDifference, ...] = (
    KnownDifference(
        "CONSUMER_SENTIMENT", "KR", "OECD",
        "different construction: the OECD indicator is amplitude-adjusted to a long-run average of 100, "
        "the Bank of Korea CCSI is not",
        "OECD DF_CLI series key component AA (amplitude adjusted) vs ECOS 511Y002 (CCSI)",
    ),
)


def known_difference(concept_id: str | None, country: str | None, provider: str) -> KnownDifference | None:
    return next(
        (k for k in KNOWN_DIFFERENCES if (k.concept_id, k.country, k.provider) == (concept_id, country, provider)),
        None,
    )


_BY_ID: dict[str, Concept] = {}
for _concept in CONCEPTS:
    for _name in (_concept.id, *_concept.aliases):
        _BY_ID[_name.upper()] = _concept


def _norm(text: str) -> str:
    return re.sub(r"[\s/_()\-]", "", text).lower()


def get_concept(identifier: str | None) -> Concept | None:
    """Exact lookup by concept id or alias (case-insensitive)."""
    if not identifier:
        return None
    return _BY_ID.get(identifier.strip().upper())


def rank_concepts(query: str) -> list[tuple[int, Concept]]:
    """Score concepts against free text (higher is better).

    exact id/alias/name/synonym: 1000 · a synonym or name inside the query: 100 + its length ·
    the query inside a synonym or name: 10 · any query word in the English name: 1.
    """
    q = _norm(query)
    if not q:
        return []
    ranked: list[tuple[int, Concept]] = []
    for c in CONCEPTS:
        keys = [_norm(k) for k in (c.id, *c.aliases, c.name_ko, c.name_en, *c.synonyms)]
        if q in keys:
            score = 1000
        elif contained := [len(k) for k in keys if len(k) >= 2 and k in q]:
            score = 100 + max(contained)
        elif any(q in k for k in keys):
            score = 10
        elif any(len(w) >= 3 and re.search(rf"\b{re.escape(w)}\b", c.name_en.lower()) for w in query.lower().split()):
            score = 1
        else:
            continue
        ranked.append((score, c))
    ranked.sort(key=lambda pair: -pair[0])
    return ranked


def find_concept(query: str) -> Concept | None:
    """Unambiguous best match for free text, else None."""
    exact = get_concept(query)
    if exact:
        return exact
    ranked = rank_concepts(query)
    if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return ranked[0][1]
    return None


def search_concepts(query: str, limit: int = 10) -> list[Concept]:
    if not query.strip():
        return list(CONCEPTS[:limit])
    return [c for _, c in rank_concepts(query)[:limit]]
