"""Canonical economic concepts layer connecting ECOS to the SDMX information model.

This layer defines standardized cross-domain concepts (e.g., POLICY_RATE, CPI_HEADLINE,
GDP_REAL, EXR_USD_KRW) and establishes two-way mappings between:
1. Canonical SDMX concept identifiers & standard dimensions (REF_AREA=KR, FREQ, UNIT_MEASURE,
   UNIT_MULT, BASE_PER, DECIMALS, ADJUSTMENT).
2. Bank of Korea ECOS time series keys (STAT_CODE, CYCLE, ITEM_CODE1..4).
3. Cross-agency international mappings (IMF IFS, OECD KEI/MEI, BIS CBPOL) for multi-agency
   statistical harmonization.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CrossAgencyMapping:
    """Mapping hooks to international statistical agencies using SDMX."""

    imf: dict[str, str] = field(default_factory=dict)   # e.g., {"dataflow": "IFS", "series_key": "KR.PCPI_IX"}
    oecd: dict[str, str] = field(default_factory=dict)  # e.g., {"dataflow": "PRICES_CPI", "series_key": "KOR.CPALTT01.IXOB.M"}
    bis: dict[str, str] = field(default_factory=dict)   # e.g., {"dataflow": "CBPOL", "series_key": "D:KR"}
    fred: str | None = None                             # Federal Reserve Economic Data series ID (e.g., "INTDSRKRM193N")
    wb: str | None = None                               # World Bank indicator ID (e.g., "FP.CPI.TOTL.ZG")


@dataclass(frozen=True)
class SdmxAttributes:
    """Standard SDMX cross-domain attributes and dimensions."""

    ref_area: str = "KR"                               # ISO-3166 alpha-2 country code
    freq: str = "M"                                    # SDMX Frequency: D, M, Q, A, S, SM
    unit_measure: str = "IX"                           # SDMX Unit of Measure: IX, PC, PC_PA, PC_YOY, KRW, USD
    unit_mult: int = 0                                 # Multiplier exponent: 0 (units), 3 (thousands), 6 (millions), 9 (billions), 12 (trillions)
    base_per: str | None = None                        # Base period for index numbers (e.g., "2020=100")
    decimals: int = 2                                  # Standard display decimals
    adjustment: str = "N"                              # Seasonal adjustment: "Y" (Adjusted), "N" (Raw/Unadjusted)
    counterpart_area: str | None = None                # Counterpart area (e.g., "US" for USD/KRW)


@dataclass(frozen=True)
class EcosMapping:
    """ECOS API series coordinates and processing hints."""

    stat_code: str
    cycle: str
    item_code1: str | None = None
    item_code2: str | None = None
    item_code3: str | None = None
    item_code4: str | None = None
    item_name1: str | None = None
    item_name2: str | None = None
    stat_name: str | None = None
    default_transform: str | None = None              # "yoy", "pop", or None
    changes_only: bool = False                        # True for step-like series such as policy rate


@dataclass(frozen=True)
class Concept:
    """Canonical economic concept definition."""

    concept_id: str                                    # e.g., "BOK_BASE_RATE", "CPI_HEADLINE"
    name_ko: str                                       # Korean name
    name_en: str                                       # English name
    category: str                                      # "interest_rate", "price", "national_accounts", "exchange_rate", "money", "bonds", "stocks", "external", "sentiment", "labor", "real_estate"
    description_ko: str
    description_en: str
    synonyms: list[str]                                # Korean and English synonyms for semantic matching
    sdmx: SdmxAttributes
    ecos: EcosMapping
    cross_agency: CrossAgencyMapping = field(default_factory=CrossAgencyMapping)

    def to_dict(self) -> dict[str, Any]:
        """Convert concept to dictionary for serialization."""
        return {
            "concept_id": self.concept_id,
            "name_ko": self.name_ko,
            "name_en": self.name_en,
            "category": self.category,
            "description_ko": self.description_ko,
            "description_en": self.description_en,
            "synonyms": self.synonyms,
            "sdmx": asdict(self.sdmx),
            "ecos": asdict(self.ecos),
            "cross_agency": asdict(self.cross_agency),
            "get_data_hint": {
                "stat_code": self.ecos.stat_code,
                "cycle": self.ecos.cycle,
                "item_code1": self.ecos.item_code1,
                "item_code2": self.ecos.item_code2,
                "transform": self.ecos.default_transform,
                "changes_only": self.ecos.changes_only,
            },
        }


# ── Canonical Concept Definitions ──────────────────────────────────────────

CONCEPTS: list[Concept] = [
    # 1. 한국은행 기준금리
    Concept(
        concept_id="BOK_BASE_RATE",
        name_ko="한국은행 기준금리",
        name_en="Bank of Korea Base Rate",
        category="interest_rate",
        description_ko="한국은행 금융통화위원회가 결정하는 정책금리로, 환매조건부채권(RP) 매매 등 통화정책 운용의 기준이 되는 금리입니다.",
        description_en="The target policy interest rate set by the Monetary Policy Board of the Bank of Korea.",
        synonyms=["기준금리", "정책금리", "금리", "base_rate", "policy_rate", "bok_base_rate", "central_bank_rate", "bok_rate"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="D",
            unit_measure="PC_PA",
            unit_mult=0,
            decimals=2,
            adjustment="N",
        ),
        ecos=EcosMapping(
            stat_code="722Y001",
            stat_name="1.3.1. 한국은행 기준금리 및 여수신금리",
            cycle="D",
            item_code1="0101000",
            item_name1="한국은행 기준금리",
            changes_only=True,
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.FP_CP_A_PA"},
            bis={"dataflow": "CBPOL", "series_key": "D:KR"},
            oecd={"dataflow": "KEI", "series_key": "KOR.IRSTCB01.ST.D"},
            fred="INTDSRKRM193N",
        ),
    ),

    # 2. 소비자물가지수 (총지수)
    Concept(
        concept_id="CPI_HEADLINE",
        name_ko="소비자물가지수(총지수)",
        name_en="Consumer Price Index (Headline)",
        category="price",
        description_ko="가구가 일상생활을 영위하기 위해 구입하는 소비재와 서비스의 가격변동을 종합적으로 측정하는 물가지수입니다 (2020=100).",
        description_en="Measures the overall change in prices of goods and services purchased by households (2020=100).",
        synonyms=["소비자물가", "소비자물가지수", "CPI", "물가", "물가지수", "cpi", "cpi_headline", "headline_cpi"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="IX",
            unit_mult=0,
            base_per="2020=100",
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="901Y009",
            stat_name="4.2.1. 소비자물가지수",
            cycle="M",
            item_code1="0",
            item_name1="총지수",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.PCPI_IX"},
            oecd={"dataflow": "PRICES_CPI", "series_key": "KOR.CPALTT01.IXOB.M"},
            fred="KORCPIALLMINMEI",
            wb="FP.CPI.TOTL",
        ),
    ),

    # 3. 소비자물가상승률 (전년동월대비, YoY)
    Concept(
        concept_id="CPI_INFLATION_RATE",
        name_ko="소비자물가상승률(전년동월대비)",
        name_en="CPI Inflation Rate (YoY)",
        category="price",
        description_ko="소비자물가지수의 전년 동월 대비 백분율 변화율(%)로, 경제 전반의 인플레이션 압력을 나타냅니다.",
        description_en="Year-on-year percentage change of the Consumer Price Index, reflecting headline inflation.",
        synonyms=["물가상승률", "인플레이션", "소비자물가상승률", "inflation", "inflation_rate", "cpi_inflation", "cpi_yoy"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="PC_YOY",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="901Y009",
            stat_name="4.2.1. 소비자물가지수",
            cycle="M",
            item_code1="0",
            item_name1="총지수",
            default_transform="yoy",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.PCPI_PCH_PA"},
            oecd={"dataflow": "PRICES_CPI", "series_key": "KOR.CPALTT01.GY.M"},
            fred="FPCPITOTLZGKOR",
            wb="FP.CPI.TOTL.ZG",
        ),
    ),

    # 4. 실질 GDP 성장률 (전기대비)
    Concept(
        concept_id="GDP_REAL_GROWTH",
        name_ko="실질 경제성장률(전기대비)",
        name_en="Real GDP Growth Rate (QoQ)",
        category="national_accounts",
        description_ko="물가변동을 제거한 실질 국내총생산(GDP)의 계절조정 전분기 대비 성장률(%)입니다.",
        description_en="Quarter-on-quarter growth rate of seasonally adjusted real gross domestic product.",
        synonyms=["경제성장률", "성장률", "GDP성장률", "실질성장률", "gdp_growth", "economic_growth", "real_gdp_growth"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="Q",
            unit_measure="PC_QOQ",
            unit_mult=0,
            decimals=2,
            adjustment="Y",
        ),
        ecos=EcosMapping(
            stat_code="200Y102",
            stat_name="2.1.1.2. 주요지표(분기지표)",
            cycle="Q",
            item_code1="10111",
            item_name1="국내총생산(GDP)(실질, 계절조정, 전기비)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.NGDP_R_K_IX"},
            oecd={"dataflow": "QNA", "series_key": "KOR.B1_GE.GPSA.Q"},
            fred="NAEXKP01KRQ652S",
        ),
    ),

    # 5. 실질 국내총생산 (GDP 수준, 십억원)
    Concept(
        concept_id="GDP_REAL",
        name_ko="실질 국내총생산(GDP)",
        name_en="Real Gross Domestic Product",
        category="national_accounts",
        description_ko="물가변동분을 제거한 기준연도 연쇄가격 기준의 실질 국내총생산(GDP) 지출 규모입니다.",
        description_en="Chained real gross domestic product in billions of Korean Won (seasonally adjusted).",
        synonyms=["실질GDP", "국내총생산", "GDP", "gdp", "real_gdp"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="Q",
            unit_measure="KRW",
            unit_mult=9,  # 십억원
            base_per="2020=100",
            decimals=1,
            adjustment="Y",
        ),
        ecos=EcosMapping(
            stat_code="200Y108",
            stat_name="2.1.2.2.2. 국내총생산에 대한 지출(계절조정, 실질, 분기)",
            cycle="Q",
            item_code1="10601",
            item_name1="국내총생산에 대한 지출",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.NGDP_R_SA_XDC"},
            oecd={"dataflow": "QNA", "series_key": "KOR.B1_GE.LRQ.Q"},
        ),
    ),

    # 6. 명목 국내총생산 (GDP 수준, 십억원)
    Concept(
        concept_id="GDP_NOMINAL",
        name_ko="명목 국내총생산(GDP)",
        name_en="Nominal Gross Domestic Product",
        category="national_accounts",
        description_ko="당해연도 가격으로 평가한 명목 국내총생산(GDP) 지출 규모입니다.",
        description_en="Nominal gross domestic product at current prices in billions of KRW.",
        synonyms=["명목GDP", "nominal_gdp"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="Q",
            unit_measure="KRW",
            unit_mult=9,
            decimals=1,
            adjustment="Y",
        ),
        ecos=EcosMapping(
            stat_code="200Y107",
            stat_name="2.1.2.2.1. 국내총생산에 대한 지출(계절조정, 명목, 분기)",
            cycle="Q",
            item_code1="10501",
            item_name1="국내총생산에 대한 지출",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.NGDP_SA_XDC"},
            oecd={"dataflow": "QNA", "series_key": "KOR.B1_GE.CPCARSA.Q"},
        ),
    ),

    # 7. 원/미국달러 환율 (일별)
    Concept(
        concept_id="EXR_USD_KRW_DAILY",
        name_ko="원/미국달러 환율(일별)",
        name_en="USD/KRW Exchange Rate (Daily)",
        category="exchange_rate",
        description_ko="서울외환시장에서 거래되는 미국 달러화 대비 원화의 일별 매매기준율(원/달러)입니다.",
        description_en="Daily basic market exchange rate of Korean Won per US Dollar.",
        synonyms=["환율", "원달러", "달러", "USD", "usd", "usdkrw", "exchange_rate", "dollar_rate"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            counterpart_area="US",
            freq="D",
            unit_measure="KRW",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="731Y001",
            stat_name="3.1.1.1. 주요국 통화의 대원화환율",
            cycle="D",
            item_code1="0000001",
            item_name1="원/미국달러(매매기준율)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.ENDA_XDC_USD_RATE"},
            bis={"dataflow": "XRU", "series_key": "D:KR:USD"},
            oecd={"dataflow": "MEI", "series_key": "KOR.CCUSMA02.ST.D"},
            fred="DEXKOUS",
        ),
    ),

    # 8. 원/미국달러 환율 (월평균)
    Concept(
        concept_id="EXR_USD_KRW_MONTHLY",
        name_ko="원/미국달러 환율(월평균)",
        name_en="USD/KRW Exchange Rate (Monthly Average)",
        category="exchange_rate",
        description_ko="해당 월 중 거래일들의 매매기준율을 산술평균한 원/달러 월평균 환율입니다.",
        description_en="Monthly average of daily basic exchange rates of KRW per USD.",
        synonyms=["월평균환율", "원달러월평균", "usd_krw_monthly"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            counterpart_area="US",
            freq="M",
            unit_measure="KRW",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="731Y004",
            stat_name="3.1.2.1. 주요국 통화의 대원화환율",
            cycle="M",
            item_code1="0000001",
            item_name1="원/미국달러(매매기준율)",
            item_code2="0000100",
            item_name2="평균자료",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.ENEA_XDC_USD_RATE"},
            bis={"dataflow": "XRU", "series_key": "M:KR:USD"},
            fred="EXKOUS",
        ),
    ),

    # 9. 원/100엔 환율 (일별)
    Concept(
        concept_id="EXR_JPY_KRW_DAILY",
        name_ko="원/100엔 환율(일별)",
        name_en="JPY/KRW Exchange Rate (per 100 Yen)",
        category="exchange_rate",
        description_ko="일본 100엔당 원화 환율(일별 매매기준율)입니다.",
        description_en="Daily basic exchange rate of Korean Won per 100 Japanese Yen.",
        synonyms=["엔화", "원엔", "JPY", "jpy", "엔달러", "jpy_krw", "yen"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            counterpart_area="JP",
            freq="D",
            unit_measure="KRW",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="731Y001",
            stat_name="3.1.1.1. 주요국 통화의 대원화환율",
            cycle="D",
            item_code1="0000002",
            item_name1="원/일본엔(100엔)",
        ),
        cross_agency=CrossAgencyMapping(
            bis={"dataflow": "XRU", "series_key": "D:KR:JPY"},
        ),
    ),

    # 10. 국고채 3년 수익률 (일별)
    Concept(
        concept_id="BOND_KTB_3Y_DAILY",
        name_ko="국고채 수익률(3년, 일별)",
        name_en="Korea Treasury Bond Yield (3-Year, Daily)",
        category="bonds",
        description_ko="국내 채권시장의 대표적인 지표금리인 3년 만기 국고채의 최종호가수익률(연%)입니다.",
        description_en="Daily final quoted yield of 3-year Korean Treasury Bonds (KTB 3Y).",
        synonyms=["국고채", "국고채3년", "채권금리", "시장금리", "ktb3y", "treasury_3y", "bond_yield"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="D",
            unit_measure="PC_PA",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="817Y002",
            stat_name="1.3.2.1. 시장금리(일별)",
            cycle="D",
            item_code1="010200000",
            item_name1="국고채(3년)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.FIGB_PA"},
            oecd={"dataflow": "KEI", "series_key": "KOR.IRLTLT01.ST.D"},
            fred="IRLTLT01KRM156N",
        ),
    ),

    # 11. 국고채 10년 수익률 (일별)
    Concept(
        concept_id="BOND_KTB_10Y_DAILY",
        name_ko="국고채 수익률(10년, 일별)",
        name_en="Korea Treasury Bond Yield (10-Year, Daily)",
        category="bonds",
        description_ko="장기 시장금리의 벤치마크 역할을 하는 10년 만기 국고채의 최종호가수익률(연%)입니다.",
        description_en="Daily final quoted yield of 10-year Korean Treasury Bonds (KTB 10Y).",
        synonyms=["국고채10년", "장기금리", "10년물", "ktb10y", "treasury_10y"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="D",
            unit_measure="PC_PA",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="817Y002",
            stat_name="1.3.2.1. 시장금리(일별)",
            cycle="D",
            item_code1="010210000",
            item_name1="국고채(10년)",
        ),
        cross_agency=CrossAgencyMapping(
            oecd={"dataflow": "KEI", "series_key": "KOR.IRLTLT01.ST.D"},
        ),
    ),

    # 12. 국고채 3년 수익률 (월평균)
    Concept(
        concept_id="BOND_KTB_3Y_MONTHLY",
        name_ko="국고채 수익률(3년, 월평균)",
        name_en="Korea Treasury Bond Yield (3-Year, Monthly Average)",
        category="bonds",
        description_ko="3년 만기 국고채의 월중 평균 최종호가수익률(연%)입니다.",
        description_en="Monthly average final quoted yield of 3-year Korean Treasury Bonds.",
        synonyms=["국고채월평균", "treasury_3y_monthly"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="PC_PA",
            unit_mult=0,
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="721Y001",
            stat_name="1.3.2.2. 시장금리(월,분기,년)",
            cycle="M",
            item_code1="5020000",
            item_name1="국고채(3년)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.FIGB_PA"},
        ),
    ),

    # 13. 광의통화 M2 (평잔, 십억원)
    Concept(
        concept_id="MONEY_M2",
        name_ko="광의통화(M2, 평잔)",
        name_en="Broad Money M2 (Period Average)",
        category="money",
        description_ko="협의통화(M1)에 준결제성 예금(정기예적금, MMF, 수익증권 등)을 포함한 광의의 통화 공급량(평잔, 십억원)입니다.",
        description_en="Broad money M2 supply including quasi-money (time & savings deposits, MMF, etc.) in billions of KRW.",
        synonyms=["M2", "통화량", "광의통화", "m2", "money_supply", "broad_money"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="KRW",
            unit_mult=9,
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="161Y006",
            stat_name="1.1.3.1.2. M2 상품별 구성내역(평잔, 원계열)",
            cycle="M",
            item_code1="BBHA00",
            item_name1="M2(평잔, 원계열)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.FM2_XDC"},
            oecd={"dataflow": "KEI", "series_key": "KOR.MANMM101.ST.M"},
            fred="MYAGM2KRM189S",
        ),
    ),

    # 14. 본원통화 (평잔, 십억원)
    Concept(
        concept_id="MONEY_BASE",
        name_ko="본원통화(평잔, 계절조정)",
        name_en="Monetary Base (Period Average, SA)",
        category="money",
        description_ko="중앙은행의 화폐발행액과 금융기관의 지급준비예치금 합계로, 통화창출의 기초가 되는 본원통화 공급량입니다.",
        description_en="Monetary base (currency issued + bank reserves held at BOK) in billions of KRW.",
        synonyms=["본원통화", "reserve_money", "monetary_base", "base_money"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="KRW",
            unit_mult=9,
            decimals=1,
            adjustment="Y",
        ),
        ecos=EcosMapping(
            stat_code="102Y004",
            stat_name="1.1.1.1.1. 본원통화 구성내역(평잔, 계절조정계열)",
            cycle="M",
            item_code1="ABA1",
            item_name1="본원통화(평잔,계절조정계열)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.FMB_XDC"},
        ),
    ),

    # 15. 생산자물가지수 (총지수)
    Concept(
        concept_id="PPI_HEADLINE",
        name_ko="생산자물가지수(총지수)",
        name_en="Producer Price Index (Headline)",
        category="price",
        description_ko="국내 생산자가 국내 시장에 공급하는 상품과 서비스의 출하 가격 변동을 종합 측정한 지수입니다 (2020=100).",
        description_en="Measures average changes in selling prices received by domestic producers for their output (2020=100).",
        synonyms=["생산자물가", "생산자물가지수", "PPI", "ppi", "producer_price_index"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="IX",
            unit_mult=0,
            base_per="2020=100",
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="404Y014",
            stat_name="4.1.1. 생산자물가지수",
            cycle="M",
            item_code1="*AA",
            item_name1="총지수",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.PPI_IX"},
            oecd={"dataflow": "MEI", "series_key": "KOR.PIEAMP01.IXOB.M"},
            fred="KORPPIALLMINMEI",
        ),
    ),

    # 16. KOSPI 주가지수 (종가)
    Concept(
        concept_id="STOCK_KOSPI",
        name_ko="KOSPI 종합주가지수(종가)",
        name_en="KOSPI Composite Index (Close)",
        category="stocks",
        description_ko="한국거래소 유가증권시장(코스피)에 상장된 모든 보통주의 시가총액 변동을 나타내는 종합주가지수입니다.",
        description_en="Composite stock price index of all common shares listed on the Korea Exchange Stock Market (1980.1.4=100).",
        synonyms=["코스피", "KOSPI", "kospi", "주가지수", "주가", "stock_index"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="D",
            unit_measure="IX",
            unit_mult=0,
            base_per="19800104=100",
            decimals=2,
        ),
        ecos=EcosMapping(
            stat_code="802Y001",
            stat_name="1.4.1.1. 주식시장(일별)",
            cycle="D",
            item_code1="0001000",
            item_name1="KOSPI지수",
        ),
        cross_agency=CrossAgencyMapping(
            oecd={"dataflow": "MEI", "series_key": "KOR.SPASTT01.IXOB.D"},
            fred="NIKKEI225",  # comparative slot
        ),
    ),

    # 17. 경상수지 (백만달러)
    Concept(
        concept_id="BOP_CURRENT_ACCOUNT",
        name_ko="경상수지",
        name_en="Current Account Balance",
        category="external",
        description_ko="한 나라가 일정 기간 외국과 행한 경제적 거래 중 상품·서비스·본원소득·이전소득수지의 합계(백만달러)입니다.",
        description_en="Sum of balance on goods, services, primary income, and secondary income in millions of USD.",
        synonyms=["경상수지", "국제수지", "current_account", "bop_ca", "balance_of_payments"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="USD",
            unit_mult=6,
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="301Y013",
            stat_name="2.2.1.1. 국제수지(월, 연)",
            cycle="M",
            item_code1="000000",
            item_name1="경상수지",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "BOP", "series_key": "KR.BCA_BP6_USD"},
            oecd={"dataflow": "MEI", "series_key": "KOR.B6BLTT02.ST.M"},
            fred="KORB6BLTT02STSAQ",
        ),
    ),

    # 18. 상품수지 (무역수지, 백만달러)
    Concept(
        concept_id="BOP_GOODS_BALANCE",
        name_ko="상품수지(무역수지)",
        name_en="Goods Balance (Trade Balance)",
        category="external",
        description_ko="수출과 수입의 차이로 발생하는 상품 거래 수지(FOB 기준, 백만달러)입니다.",
        description_en="Net exports minus imports of goods (FOB basis) in millions of USD.",
        synonyms=["상품수지", "무역수지", "trade_balance", "goods_balance"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="USD",
            unit_mult=6,
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="301Y013",
            stat_name="2.2.1.1. 국제수지(월, 연)",
            cycle="M",
            item_code1="100000",
            item_name1="상품수지",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "BOP", "series_key": "KR.BXG_BP6_USD"},
        ),
    ),

    # 19. 외환보유액 (천달러)
    Concept(
        concept_id="FOREIGN_RESERVES",
        name_ko="외환보유액",
        name_en="Foreign Exchange Reserves",
        category="external",
        description_ko="중앙은행과 정부가 긴급 대외지급을 위해 보유하고 있는 외화표시 유동성 자산 총액(천달러)입니다.",
        description_en="Total official foreign exchange reserves held by the Bank of Korea and the government.",
        synonyms=["외환보유액", "외환보유고", "외환", "외환보유", "foreign_reserves", "fx_reserves"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="USD",
            unit_mult=3,
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="732Y001",
            stat_name="3.2.1. 외환보유액",
            cycle="M",
            item_code1="99",
            item_name1="총외환보유액",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.RAFA_USD"},
            wb="FI.RES.TOTL.CD",
        ),
    ),

    # 20. 소비자심리지수 (CCSI)
    Concept(
        concept_id="SENTIMENT_CCSI",
        name_ko="소비자심리지수(CCSI)",
        name_en="Composite Consumer Sentiment Index",
        category="sentiment",
        description_ko="가계의 경제상황에 대한 전반적 인식을 나타내는 종합지수로, 100 초과는 낙관적, 100 미만은 비관적을 의미합니다.",
        description_en="Comprehensive index measuring consumer economic expectations; >100 optimistic, <100 pessimistic.",
        synonyms=["소비자심리지수", "CCSI", "소비자심리", "소비심리", "ccsi", "consumer_sentiment"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="IX",
            unit_mult=0,
            base_per="100=장기평균",
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="511Y002",
            stat_name="6.1.1.2. 소비자심리지수(CSI)",
            cycle="M",
            item_code1="99988",
            item_name1="소비자심리지수",
        ),
        cross_agency=CrossAgencyMapping(
            oecd={"dataflow": "MEI", "series_key": "KOR.CSCICP03.M"},
            fred="CSCICP03KRM665S",
        ),
    ),

    # 21. 경제심리지수 (ESI)
    Concept(
        concept_id="SENTIMENT_ESI",
        name_ko="경제심리지수(ESI)",
        name_en="Economic Sentiment Index",
        category="sentiment",
        description_ko="기업경기실사지수(BSI)와 소비자동향지수(CSI)를 합성하여 민간 경제주체의 경제 심리를 종합적으로 측정한 지표입니다.",
        description_en="Synthesized index combining business and consumer sentiment to gauge overall economic mood.",
        synonyms=["경제심리지수", "ESI", "경제심리", "esi", "economic_sentiment"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="IX",
            unit_mult=0,
            base_per="100=장기평균",
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="513Y001",
            stat_name="6.3. 경제심리지수",
            cycle="M",
            item_code1="99988",
            item_name1="경제심리지수",
        ),
        cross_agency=CrossAgencyMapping(),
    ),

    # 22. 실업률 (계절조정, %)
    Concept(
        concept_id="LABOR_UNEMPLOYMENT_RATE",
        name_ko="실업률(계절조정)",
        name_en="Unemployment Rate (Seasonally Adjusted)",
        category="labor",
        description_ko="경제활동인구 중 실업자가 차지하는 비율(%)로, 노동시장의 유휴 인력 현황을 나타냅니다.",
        description_en="Percentage of the labor force that is unemployed (seasonally adjusted).",
        synonyms=["실업률", "고용률", "unemployment", "unemployment_rate", "jobless_rate"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="PC",
            unit_mult=0,
            decimals=1,
            adjustment="Y",
        ),
        ecos=EcosMapping(
            stat_code="901Y027",
            stat_name="8.2.1. 실업률",
            cycle="M",
            item_code1="I61B",
            item_name1="실업률(계절조정)",
        ),
        cross_agency=CrossAgencyMapping(
            imf={"dataflow": "IFS", "series_key": "KR.LUR_PT"},
            oecd={"dataflow": "MEI", "series_key": "KOR.LRHUTTTT.STSA.M"},
            fred="LRHUTTTTKRM156S",
            wb="SL.UEM.TOTL.ZS",
        ),
    ),

    # 23. 전국 주택매매가격지수
    Concept(
        concept_id="HOUSING_PRICE_INDEX",
        name_ko="전국 주택매매가격지수",
        name_en="National Housing Purchase Price Index",
        category="real_estate",
        description_ko="전국 주택(아파트·연립·단독 등)의 매매가격 변동을 종합 측정한 지수입니다 (2021.06=100).",
        description_en="Index measuring the purchase price changes of residential properties nationwide.",
        synonyms=["주택가격", "집값", "부동산", "아파트가격", "housing_price", "house_price", "real_estate"],
        sdmx=SdmxAttributes(
            ref_area="KR",
            freq="M",
            unit_measure="IX",
            unit_mult=0,
            base_per="202106=100",
            decimals=1,
        ),
        ecos=EcosMapping(
            stat_code="901Y062",
            stat_name="4.3.1. 주택매매가격지수",
            cycle="M",
            item_code1="R01",
            item_name1="종합",
        ),
        cross_agency=CrossAgencyMapping(
            bis={"dataflow": "SPP", "series_key": "M:KR:N:628"},
            oecd={"dataflow": "RHPI", "series_key": "KOR.IXOB.M"},
        ),
    ),
]

_CONCEPTS_BY_ID: dict[str, Concept] = {c.concept_id.upper(): c for c in CONCEPTS}


def get_concept(identifier: str) -> Concept | None:
    """Retrieve concept by exact canonical ID (case-insensitive) or primary name."""
    clean = identifier.strip().upper()
    if clean in _CONCEPTS_BY_ID:
        return _CONCEPTS_BY_ID[clean]
    for c in CONCEPTS:
        if clean in (c.concept_id.upper(), c.name_ko.upper(), c.name_en.upper()):
            return c
    return None


def search_concepts(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search canonical concepts by Korean/English name, ID, or synonyms.

    Returns ranked concept dictionaries.
    """
    text = query.strip().lower()
    if not text:
        return [c.to_dict() for c in CONCEPTS[:limit]]

    needle = re.sub(r"\s+", "", text)
    matches: list[tuple[int, Concept]] = []

    for c in CONCEPTS:
        c_id = c.concept_id.lower()
        c_ko = re.sub(r"\s+", "", c.name_ko.lower())
        c_en = c.name_en.lower()
        synonyms = [re.sub(r"\s+", "", s.lower()) for s in c.synonyms]

        score = -1
        # Exact match
        if needle in (c_id, c_ko) or text in (c_id, c_en):
            score = 0
        elif needle in synonyms or text in synonyms:
            score = 1
        elif needle in c_ko or text in c_en:
            score = 2
        elif any(needle in s for s in synonyms):
            score = 3
        elif any(word in c_ko or word in c_en for word in text.split()):
            score = 4

        if score >= 0:
            matches.append((score, c))

    matches.sort(key=lambda x: x[0])
    return [c.to_dict() for _, c in matches[:limit]]


def all_concepts() -> list[dict[str, Any]]:
    """Return all canonical concepts as dictionary list."""
    return [c.to_dict() for c in CONCEPTS]
