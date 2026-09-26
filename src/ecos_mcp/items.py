"""Pre-indexed item and commodity catalog for fine-grained statistical searches.

Enables semantic and item-level discovery across high-impact ECOS tables
(e.g., searching for "쌀", "휘발유", "반도체", "전기료", "사과", "아파트").
"""

from __future__ import annotations

import re
from typing import Any

# Catalog of key sub-items across major economic domains
STATISTICAL_ITEMS: list[dict[str, Any]] = [
    # ── 농축수산물 및 식료품 (소비자물가지수 901Y009) ──────────────────────────
    {
        "item_name": "쌀",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01111",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["rice", "백미", "곡물", "밥"],
    },
    {
        "item_name": "현미",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01112",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["brown_rice", "곡물"],
    },
    {
        "item_name": "찹쌀",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01113",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["glutinous_rice"],
    },
    {
        "item_name": "콩",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01121",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["대두", "soybean"],
    },
    {
        "item_name": "감자",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01131",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["potato"],
    },
    {
        "item_name": "고구마",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01132",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["sweet_potato"],
    },
    {
        "item_name": "배추",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01141",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["cabbage", "kimchi_cabbage", "채소"],
    },
    {
        "item_name": "무",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01143",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["radish", "채소"],
    },
    {
        "item_name": "사과",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01151",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["apple", "과일"],
    },
    {
        "item_name": "배",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01152",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["pear", "과일"],
    },
    {
        "item_name": "쇠고기(국산)",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01161",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["한우", "소고기", "beef", "국산쇠고기"],
    },
    {
        "item_name": "돼지고기",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01162",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["삼겹살", "pork", "육류"],
    },
    {
        "item_name": "닭고기",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01163",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["chicken", "치킨"],
    },
    {
        "item_name": "달걀",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01171",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["계란", "egg", "eggs"],
    },
    {
        "item_name": "우유",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01172",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["milk"],
    },
    {
        "item_name": "라면",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01181",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["ramen", "instant_noodles", "면류"],
    },
    {
        "item_name": "빵",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01182",
        "unit": "2020=100",
        "category": "식료품",
        "synonyms": ["bread", "베이커리"],
    },
    {
        "item_name": "커피",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "01211",
        "unit": "2020=100",
        "category": "음료",
        "synonyms": ["coffee", "원두"],
    },

    # ── 에너지 및 공공요금 ──────────────────────────────────────────────────
    {
        "item_name": "휘발유",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "04511",
        "unit": "2020=100",
        "category": "에너지",
        "synonyms": ["gasoline", "petrol", "기름값", "유류"],
    },
    {
        "item_name": "경유",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "04512",
        "unit": "2020=100",
        "category": "에너지",
        "synonyms": ["diesel", "디젤", "유류"],
    },
    {
        "item_name": "도시가스",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "04411",
        "unit": "2020=100",
        "category": "공공요금",
        "synonyms": ["city_gas", "가스비", "난방비", "lng"],
    },
    {
        "item_name": "전기료",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "04412",
        "unit": "2020=100",
        "category": "공공요금",
        "synonyms": ["electricity", "전기요금", "전기세"],
    },
    {
        "item_name": "상수도료",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "04413",
        "unit": "2020=100",
        "category": "공공요금",
        "synonyms": ["water", "수도요금", "수도세"],
    },
    {
        "item_name": "아파트관리비",
        "stat_code": "901Y009",
        "stat_name": "4.2.1. 소비자물가지수",
        "cycle": "M",
        "item_code": "04421",
        "unit": "2020=100",
        "category": "주거",
        "synonyms": ["maintenance_fee", "관리비"],
    },

    # ── 생산자물가지수 주요 품목 (404Y014) ──────────────────────────────────
    {
        "item_name": "쌀(생산자)",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1011101",
        "unit": "2020=100",
        "category": "농림수산품",
        "synonyms": ["쌀", "rice_ppi"],
    },
    {
        "item_name": "휘발유(생산자)",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1031101",
        "unit": "2020=100",
        "category": "석유제품",
        "synonyms": ["휘발유", "gasoline_ppi"],
    },
    {
        "item_name": "DRAM 반도체",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1071101",
        "unit": "2020=100",
        "category": "IT·전자",
        "synonyms": ["반도체", "DRAM", "dram", "semiconductor", "디램", "메모리"],
    },
    {
        "item_name": "플래시메모리 반도체",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1071102",
        "unit": "2020=100",
        "category": "IT·전자",
        "synonyms": ["nand", "낸드플래시", "flash_memory", "반도체"],
    },
    {
        "item_name": "OLED 디스플레이",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1071201",
        "unit": "2020=100",
        "category": "IT·전자",
        "synonyms": ["oled", "display", "디스플레이", "패널"],
    },
    {
        "item_name": "승용차",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1081101",
        "unit": "2020=100",
        "category": "자동차",
        "synonyms": ["automobile", "자동차", "차량"],
    },
    {
        "item_name": "열연강판(철강)",
        "stat_code": "404Y014",
        "stat_name": "4.1.1. 생산자물가지수",
        "cycle": "M",
        "item_code": "1051101",
        "unit": "2020=100",
        "category": "철강",
        "synonyms": ["철강", "열연", "steel", "강판"],
    },

    # ── 주요 시장금리 세부 만기별 (817Y002) ──────────────────────────────────
    {
        "item_name": "콜금리(1일물)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010101000",
        "unit": "연%",
        "category": "단기금리",
        "synonyms": ["콜금리", "call_rate", "단기금리"],
    },
    {
        "item_name": "양도성예금증서(CD 91일)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010502000",
        "unit": "연%",
        "category": "단기금리",
        "synonyms": ["CD", "CD91일", "cd_rate", "양도성예금증서"],
    },
    {
        "item_name": "기업어음(CP 91일)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010503000",
        "unit": "연%",
        "category": "단기금리",
        "synonyms": ["CP", "CP91일", "cp_rate", "기업어음"],
    },
    {
        "item_name": "국고채(1년)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010190000",
        "unit": "연%",
        "category": "채권금리",
        "synonyms": ["국고채1년", "ktb1y"],
    },
    {
        "item_name": "국고채(2년)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010195000",
        "unit": "연%",
        "category": "채권금리",
        "synonyms": ["국고채2년", "ktb2y"],
    },
    {
        "item_name": "국고채(5년)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010200001",
        "unit": "연%",
        "category": "채권금리",
        "synonyms": ["국고채5년", "ktb5y"],
    },
    {
        "item_name": "국고채(20년)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010220000",
        "unit": "연%",
        "category": "채권금리",
        "synonyms": ["국고채20년", "ktb20y", "초장기채"],
    },
    {
        "item_name": "국고채(30년)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010230000",
        "unit": "연%",
        "category": "채권금리",
        "synonyms": ["국고채30년", "ktb30y", "초장기채"],
    },
    {
        "item_name": "회사채(3년, AA-)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010300000",
        "unit": "연%",
        "category": "회사채",
        "synonyms": ["회사채", "회사채3년", "우량회사채", "corporate_bond"],
    },
    {
        "item_name": "회사채(3년, BBB-)",
        "stat_code": "817Y002",
        "stat_name": "1.3.2.1. 시장금리(일별)",
        "cycle": "D",
        "item_code": "010320000",
        "unit": "연%",
        "category": "회사채",
        "synonyms": ["BBB", "투기등급회사채", "신용스프레드"],
    },

    # ── 주요 통화별 환율 (731Y001) ──────────────────────────────────────────
    {
        "item_name": "원/중국위안(매매기준율)",
        "stat_code": "731Y001",
        "stat_name": "3.1.1.1. 주요국 통화의 대원화환율",
        "cycle": "D",
        "item_code": "0000053",
        "unit": "원",
        "category": "환율",
        "synonyms": ["위안화", "중국위안", "CNY", "cny", "위안"],
    },
    {
        "item_name": "원/유로(매매기준율)",
        "stat_code": "731Y001",
        "stat_name": "3.1.1.1. 주요국 통화의 대원화환율",
        "cycle": "D",
        "item_code": "0000003",
        "unit": "원",
        "category": "환율",
        "synonyms": ["유로", "유로화", "EUR", "eur"],
    },
    {
        "item_name": "원/영국파운드(매매기준율)",
        "stat_code": "731Y001",
        "stat_name": "3.1.1.1. 주요국 통화의 대원화환율",
        "cycle": "D",
        "item_code": "0000004",
        "unit": "원",
        "category": "환율",
        "synonyms": ["파운드", "영국파운드", "GBP", "gbp"],
    },
    {
        "item_name": "원/호주달러(매매기준율)",
        "stat_code": "731Y001",
        "stat_name": "3.1.1.1. 주요국 통화의 대원화환율",
        "cycle": "D",
        "item_code": "0000005",
        "unit": "원",
        "category": "환율",
        "synonyms": ["호주달러", "AUD", "aud"],
    },

    # ── 부동산 및 주택 (901Y062, 901Y063) ───────────────────────────────────
    {
        "item_name": "전국 아파트 매매가격지수",
        "stat_code": "901Y062",
        "stat_name": "4.3.1. 주택매매가격지수",
        "cycle": "M",
        "item_code": "R02",
        "unit": "202106=100",
        "category": "부동산",
        "synonyms": ["아파트매매", "아파트가격", "아파트값", "apartment_price"],
    },
    {
        "item_name": "전국 아파트 전세가격지수",
        "stat_code": "901Y063",
        "stat_name": "4.3.2. 주택전세가격지수",
        "cycle": "M",
        "item_code": "R02",
        "unit": "202106=100",
        "category": "부동산",
        "synonyms": ["전세", "전세가격", "아파트전세", "jeonse"],
    },
]


def search_items(query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Search pre-indexed items and commodities by keyword or synonym.

    Returns ranked list of matching items with ready-to-use get_data parameters.
    """
    text = query.strip().lower()
    if not text:
        return [
            {
                **item,
                "get_data_example": {
                    "stat_code": item["stat_code"],
                    "cycle": item["cycle"],
                    "item_code1": item["item_code"],
                },
            }
            for item in STATISTICAL_ITEMS[:limit]
        ]

    needle = re.sub(r"\s+", "", text)
    matches: list[tuple[int, dict[str, Any]]] = []

    for item in STATISTICAL_ITEMS:
        name = re.sub(r"\s+", "", item["item_name"].lower())
        cat = item.get("category", "").lower()
        synonyms = [re.sub(r"\s+", "", s.lower()) for s in item.get("synonyms", [])]

        score = -1
        # Exact match on item_name or synonym
        if needle == name or any(needle == s for s in synonyms):
            score = 0
        elif name.startswith(needle) or any(s.startswith(needle) for s in synonyms):
            score = 1
        elif needle in name or any(needle in s for s in synonyms):
            score = 2
        elif needle in cat or any(word in name for word in text.split()):
            score = 3

        if score >= 0:
            matches.append(
                (
                    score,
                    {
                        "item_name": item["item_name"],
                        "stat_code": item["stat_code"],
                        "stat_name": item["stat_name"],
                        "cycle": item["cycle"],
                        "item_code": item["item_code"],
                        "unit": item["unit"],
                        "category": item["category"],
                        "get_data_example": {
                            "stat_code": item["stat_code"],
                            "cycle": item["cycle"],
                            "item_code1": item["item_code"],
                        },
                    },
                )
            )

    matches.sort(key=lambda x: x[0])
    return [item for _, item in matches[:limit]]
