"""Shared fixtures: an in-memory fake of the ECOS Open API served through respx."""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx
import pytest
import respx

TEST_KEY = "TESTKEY-1234567890"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeEcos:
    """Minimal ECOS emulation: StatisticSearch over registered series, paged lists otherwise."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.lists: dict[str, list[dict[str, Any]]] = {}
        # Per-argument responses: (service, first extra path segment) -> rows
        self.keyed: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.calls: list[list[str]] = []
        self.forced: list[httpx.Response | Exception] = []

    def add_series(
        self,
        stat_code: str,
        cycle: str,
        points: dict[str, Any],
        item_code1: str = "0",
        item_name1: str = "총지수",
        unit: str = "2020=100",
        stat_name: str = "테스트 통계표",
        item_code2: str | None = None,
        item_name2: str | None = None,
    ) -> None:
        for time, value in points.items():
            self.observations.append(
                {
                    "STAT_CODE": stat_code,
                    "STAT_NAME": stat_name,
                    "CYCLE": cycle,
                    "ITEM_CODE1": item_code1,
                    "ITEM_NAME1": item_name1,
                    "ITEM_CODE2": item_code2,
                    "ITEM_NAME2": item_name2,
                    "ITEM_CODE3": None,
                    "ITEM_NAME3": None,
                    "ITEM_CODE4": None,
                    "ITEM_NAME4": None,
                    "UNIT_NAME": unit,
                    "WGT": None,
                    "TIME": time,
                    "DATA_VALUE": str(value),
                }
            )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.forced:
            outcome = self.forced.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        parts = [urllib.parse.unquote(p) for p in request.url.raw_path.decode().split("/")[2:]]
        self.calls.append(parts)
        service, _key, _fmt, _lang, start, end, *extra = parts
        start_i, end_i = int(start), int(end)

        if service == "StatisticSearch":
            stat_code, cycle, first, last, *items = extra
            rows = [
                {k: v for k, v in o.items() if k != "CYCLE"}
                for o in self.observations
                if o["STAT_CODE"] == stat_code
                and o["CYCLE"] == cycle
                and first <= o["TIME"] <= last
                and all(
                    code == "?" or o[f"ITEM_CODE{i}"] == code
                    for i, code in enumerate(items, start=1)
                )
            ]
            rows.sort(key=lambda r: (r["TIME"], r["ITEM_CODE1"], r["ITEM_CODE2"] or ""))
        elif extra and (service, extra[0]) in self.keyed:
            rows = self.keyed[(service, extra[0])]
        else:
            rows = self.lists.get(service, [])

        if not rows:
            return httpx.Response(
                200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}
            )
        return httpx.Response(
            200,
            json={service: {"list_total_count": len(rows), "row": rows[start_i - 1 : end_i]}},
        )


@pytest.fixture
def fake_ecos():
    fake = FakeEcos()
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://ecos.bok.or.kr/api/").mock(side_effect=fake.handler)
        yield fake


def monthly(start_year: int, values: list[float]) -> dict[str, float]:
    """Map consecutive months starting at January of start_year to values."""
    return {
        f"{start_year + i // 12}{i % 12 + 1:02d}": v for i, v in enumerate(values)
    }


def item_row(stat_code, group, code, name, cycle="M", unit="원", parent=None, grp_name=None, start="200001", end="202608"):
    """A StatisticItemList row."""
    return {
        "STAT_CODE": stat_code,
        "STAT_NAME": "테스트 통계표",
        "GRP_CODE": f"Group{group}",
        "GRP_NAME": grp_name or ("계정항목" if group == 1 else "측정항목"),
        "ITEM_CODE": code,
        "ITEM_NAME": name,
        "P_ITEM_CODE": parent,
        "P_ITEM_NAME": None,
        "CYCLE": cycle,
        "START_TIME": start,
        "END_TIME": end,
        "DATA_CNT": 100,
        "UNIT_NAME": unit,
        "WEIGHT": None,
    }
