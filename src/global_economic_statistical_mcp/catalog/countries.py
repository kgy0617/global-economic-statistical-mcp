"""Country codes used to fill provider key templates ({ISO2}, {ISO3}, {CUR})."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    iso2: str
    iso3: str
    currency: str
    name_ko: str
    name_en: str


_COUNTRIES = [
    Country("KR", "KOR", "KRW", "한국", "Korea"),
    Country("US", "USA", "USD", "미국", "United States"),
    Country("JP", "JPN", "JPY", "일본", "Japan"),
    Country("CN", "CHN", "CNY", "중국", "China"),
    Country("GB", "GBR", "GBP", "영국", "United Kingdom"),
    Country("DE", "DEU", "EUR", "독일", "Germany"),
    Country("FR", "FRA", "EUR", "프랑스", "France"),
    Country("IT", "ITA", "EUR", "이탈리아", "Italy"),
    Country("ES", "ESP", "EUR", "스페인", "Spain"),
    Country("NL", "NLD", "EUR", "네덜란드", "Netherlands"),
    Country("BE", "BEL", "EUR", "벨기에", "Belgium"),
    Country("AT", "AUT", "EUR", "오스트리아", "Austria"),
    Country("IE", "IRL", "EUR", "아일랜드", "Ireland"),
    Country("PT", "PRT", "EUR", "포르투갈", "Portugal"),
    Country("GR", "GRC", "EUR", "그리스", "Greece"),
    Country("FI", "FIN", "EUR", "핀란드", "Finland"),
    Country("CA", "CAN", "CAD", "캐나다", "Canada"),
    Country("AU", "AUS", "AUD", "호주", "Australia"),
    Country("NZ", "NZL", "NZD", "뉴질랜드", "New Zealand"),
    Country("CH", "CHE", "CHF", "스위스", "Switzerland"),
    Country("SE", "SWE", "SEK", "스웨덴", "Sweden"),
    Country("NO", "NOR", "NOK", "노르웨이", "Norway"),
    Country("DK", "DNK", "DKK", "덴마크", "Denmark"),
    Country("PL", "POL", "PLN", "폴란드", "Poland"),
    Country("CZ", "CZE", "CZK", "체코", "Czechia"),
    Country("HU", "HUN", "HUF", "헝가리", "Hungary"),
    Country("IL", "ISR", "ILS", "이스라엘", "Israel"),
    Country("TR", "TUR", "TRY", "튀르키예", "Türkiye"),
    Country("MX", "MEX", "MXN", "멕시코", "Mexico"),
    Country("CL", "CHL", "CLP", "칠레", "Chile"),
    Country("CO", "COL", "COP", "콜롬비아", "Colombia"),
    Country("BR", "BRA", "BRL", "브라질", "Brazil"),
    Country("AR", "ARG", "ARS", "아르헨티나", "Argentina"),
    Country("IN", "IND", "INR", "인도", "India"),
    Country("ID", "IDN", "IDR", "인도네시아", "Indonesia"),
    Country("TH", "THA", "THB", "태국", "Thailand"),
    Country("MY", "MYS", "MYR", "말레이시아", "Malaysia"),
    Country("PH", "PHL", "PHP", "필리핀", "Philippines"),
    Country("VN", "VNM", "VND", "베트남", "Viet Nam"),
    Country("SG", "SGP", "SGD", "싱가포르", "Singapore"),
    Country("HK", "HKG", "HKD", "홍콩", "Hong Kong, China"),
    Country("SA", "SAU", "SAR", "사우디아라비아", "Saudi Arabia"),
    Country("ZA", "ZAF", "ZAR", "남아프리카공화국", "South Africa"),
    Country("RU", "RUS", "RUB", "러시아", "Russia"),
]

_BY_CODE: dict[str, Country] = {}
for _c in _COUNTRIES:
    for _key in (_c.iso2, _c.iso3, _c.name_ko, _c.name_en.upper()):
        _BY_CODE[_key.upper()] = _c
_BY_CODE.update({"KOREA": _BY_CODE["KR"], "SOUTH KOREA": _BY_CODE["KR"], "USA": _BY_CODE["US"], "대한민국": _BY_CODE["KR"]})


def get_country(code_or_name: str | None) -> Country | None:
    if not code_or_name:
        return None
    return _BY_CODE.get(code_or_name.strip().upper())


def iso2(code: str | None) -> str | None:
    """Normalise ISO2/ISO3 codes to ISO2; unknown codes are returned unchanged."""
    if not code:
        return None
    country = get_country(code)
    return country.iso2 if country else code.upper()


def all_countries() -> list[Country]:
    return list(_COUNTRIES)
