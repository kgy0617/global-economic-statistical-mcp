"""Shared fixtures: an in-memory fake of the ECOS Open API served through respx."""

from __future__ import annotations

import gzip
import json
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
        # 1-based request number → response to return instead (e.g. fail only the 2nd request)
        self.fail_on: dict[int, httpx.Response] = {}
        self._count = 0

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
        self._count += 1
        if self._count in self.fail_on:
            return self.fail_on[self._count]
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


# ── Fake SDMX providers (OECD / IMF / BIS / ECB / Eurostat) ─────────

SDMX_BASES = {
    "OECD": "https://sdmx.oecd.org/public/rest/",
    "IMF": "https://api.imf.org/external/sdmx/3.0/",
    "BIS": "https://stats.bis.org/api/",
    "ECB": "https://data-api.ecb.europa.eu/service/",
    "EUROSTAT": "https://ec.europa.eu/eurostat/api/dissemination/sdmx/3.0/",
}
DATA360_BASE = "https://data360api.worldbank.org/data360/"


class FakeSdmx:
    """Serves registered series as SDMX-JSON AllDimensions messages.

    OECD and IMF answer in SDMX-JSON 2.0 (``data.structures``), BIS and ECB in 1.0
    (``data.structure``), Eurostat in gzip-compressed SDMX-CSV 2.0 without a Content-Encoding
    header, with lower-case dimension ids, like the real APIs. IMF periods are written as
    ``2026-M01``. Set ``ignore_wildcard_keys`` to reproduce IMF returning every series when the
    key contains ``*``. A structure registered as a string is served as SDMX-ML.
    """

    def __init__(self) -> None:
        self.series: list[dict[str, Any]] = []
        self.structures: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.ignore_wildcard_keys = False
        self.forced: list[httpx.Response] = []

    def add(self, provider: str, flow: str, dims: dict[str, str], points: dict[str, float], attrs: dict[str, str] | None = None, name: str | None = "Test flow") -> None:
        self.series.append({"provider": provider, "flow": flow, "dims": dims, "points": points, "attrs": attrs or {}, "name": name})

    @staticmethod
    def _parse(provider: str, url: httpx.URL) -> tuple[str, str, str | None, str | None]:
        path = urllib.parse.unquote(url.path)
        params = dict(url.params)
        if provider in ("IMF", "EUROSTAT"):
            # .../sdmx/3.0/data/dataflow/{agency}/{flow}/{version}/{key}
            parts = path.split("/data/dataflow/")[1].split("/")
            flow, key = parts[1], parts[3]
            bounds = params.get("c[TIME_PERIOD]", "")
            start = next((b[3:] for b in bounds.replace(" ", "+").split("+") if b.startswith("ge:")), None)
            end = next((b[3:] for b in bounds.replace(" ", "+").split("+") if b.startswith("le:")), None)
        else:
            # .../data/{agency},{flow},{version}/{key}
            ref, key = path.split("/data/")[1].split("/", 1)
            flow = ref.split(",")[1]
            start, end = params.get("startPeriod"), params.get("endPeriod")
        return flow, key, start, end

    @staticmethod
    def _matches(dims: dict[str, str], key: str) -> bool:
        for value, part in zip(dims.values(), key.split(".")):
            if part and part != "*" and value not in part.replace(",", "+").split("+"):
                return False
        return True

    def handler(self, provider: str, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.forced:
            return self.forced.pop(0)
        if "/structure/" in request.url.path or "/dataflow/" in request.url.path and "/data/" not in request.url.path:
            flow = next((f for f in self.structures if f"/{f}/" in request.url.path or request.url.path.endswith(f"/{f}")), None)
            if not flow:
                return httpx.Response(404)
            doc = self.structures[flow]
            return httpx.Response(200, text=doc) if isinstance(doc, str) else httpx.Response(200, json=doc)
        flow, key, start, end = self._parse(provider, request.url)
        wildcard = "*" in key
        chosen = [
            s for s in self.series
            if s["provider"] == provider and s["flow"] == flow
            and (self.ignore_wildcard_keys and wildcard or self._matches(s["dims"], key))
        ]
        rows = []
        for s in chosen:
            for period, value in s["points"].items():
                canonical = period.replace("-M", "-")
                if (start and canonical < start) or (end and canonical > end):
                    continue
                rows.append((s, period, value))
        if not rows:
            return httpx.Response(404, text="NoResultsFound")
        if provider == "EUROSTAT":
            return httpx.Response(200, content=gzip.compress(self._csv(rows).encode()))
        return httpx.Response(200, json=self._message(provider, rows))

    @staticmethod
    def _csv(rows: list[tuple[dict[str, Any], str, float]]) -> str:
        dim_ids = list(rows[0][0]["dims"])
        lines = ["STRUCTURE,STRUCTURE_ID," + ",".join(d.lower() for d in dim_ids) + ",TIME_PERIOD,OBS_VALUE,OBS_FLAG,CONF_STATUS"]
        for s, period, value in rows:
            lines.append(f"dataflow,ESTAT:{s['flow']}(1.0)," + ",".join(s["dims"].values()) + f",{period},{value},{s['attrs'].get('OBS_FLAG', '')},")
        return "\r\n".join(lines) + "\r\n"

    def _message(self, provider: str, rows: list[tuple[dict[str, Any], str, float]]) -> dict[str, Any]:
        dim_ids = list(rows[0][0]["dims"])
        values: dict[str, list[str]] = {d: [] for d in [*dim_ids, "TIME_PERIOD"]}
        attr_ids = sorted({a for s, _, _ in rows for a in s["attrs"]})
        attr_values: dict[str, list[str]] = {a: [] for a in attr_ids}
        observations = {}
        for s, period, value in rows:
            idx = []
            for d in dim_ids:
                v = s["dims"][d]
                if v not in values[d]:
                    values[d].append(v)
                idx.append(values[d].index(v))
            if period not in values["TIME_PERIOD"]:
                values["TIME_PERIOD"].append(period)
            idx.append(values["TIME_PERIOD"].index(period))
            attrs = []
            for a in attr_ids:
                v = s["attrs"].get(a)
                if v is None:
                    attrs.append(None)
                    continue
                if v not in attr_values[a]:
                    attr_values[a].append(v)
                attrs.append(attr_values[a].index(v))
            observations[":".join(map(str, idx))] = [str(value) if provider == "IMF" else value, *attrs]
        structure = {
            **({"name": rows[0][0]["name"]} if rows[0][0]["name"] else {}),  # IMF sends no names
            "dimensions": {
                "observation": [
                    {"id": d, "values": [{"id": v, "name": f"{d}:{v}"} for v in values[d]]} for d in dim_ids
                ]
                + [{"id": "TIME_PERIOD", "values": [{"id": p, "name": p} for p in values["TIME_PERIOD"]]}]
            },
            "attributes": {"observation": [{"id": a, "values": [{"id": v} for v in attr_values[a]]} for a in attr_ids]},
        }
        dataset = {"action": "Information", "observations": observations}
        if provider in ("BIS", "ECB"):
            return {"meta": {}, "data": {"structure": structure, "dataSets": [dataset]}}
        return {"meta": {}, "data": {"structures": [structure], "dataSets": [{**dataset, "structure": 0}]}}


class FakeData360:
    """World Bank Data360: GET /data (paged with skip/top), POST /metadata and /searchv2."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.names: dict[str, str] = {}
        self.calls: list[str] = []

    def add(self, indicator: str, area: str, points: dict[str, float], unit: str = "PC_A", database: str = "WB_WDI", name: str | None = None) -> None:
        for period, value in points.items():
            self.rows.append(
                {
                    "DATABASE_ID": database, "INDICATOR": indicator, "REF_AREA": area, "SEX": "_T", "AGE": "_T",
                    "URBANISATION": "_T", "COMP_BREAKDOWN_1": "_Z", "COMP_BREAKDOWN_2": "_Z", "COMP_BREAKDOWN_3": "_Z",
                    "TIME_PERIOD": period, "FREQ": "A", "OBS_VALUE": str(value), "UNIT_MEASURE": unit, "UNIT_MULT": 0,
                    "OBS_STATUS": "A",
                }
            )
        if name:
            self.names[indicator] = name

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url}")
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content or b"{}")
            if path.endswith("/metadata"):
                indicator = body["query"].split("'")[1]
                if indicator not in self.names:
                    return httpx.Response(200, json={"value": []})
                meta = {"idno": indicator, "name": self.names[indicator], "database_id": "WB_WDI", "periodicity": "Annual",
                        "measurement_unit": "%", "definition_short": f"{self.names[indicator]} definition"}
                return httpx.Response(200, json={"value": [{"series_description": meta}]})
            if path.endswith("/searchv2"):
                words = body["search"].lower().split()
                hits = [{"series_description": {"idno": i, "name": n, "database_id": "WB_WDI"}}
                        for i, n in self.names.items() if all(w in n.lower() for w in words)]
                return httpx.Response(200, json={"value": hits[: body.get("top", 10)]})
            return httpx.Response(404)
        params = dict(request.url.params)
        areas = set(params.get("REF_AREA", "").split(",")) - {""}
        rows = [
            r for r in self.rows
            if r["INDICATOR"] == params.get("INDICATOR") and r["DATABASE_ID"] == params.get("DATABASE_ID")
            and (not areas or r["REF_AREA"] in areas)
            and params.get("timePeriodFrom", "0000") <= r["TIME_PERIOD"] <= params.get("timePeriodTo", "9999")
        ]
        skip, top = int(params.get("skip", 0)), int(params.get("top", 1000))
        return httpx.Response(200, json={"count": len(rows), "value": rows[skip : skip + top]})


@pytest.fixture
def fake_sdmx():
    fake = FakeSdmx()
    with respx.mock(assert_all_called=False) as router:
        for provider, base in SDMX_BASES.items():
            router.get(url__startswith=base).mock(side_effect=lambda request, p=provider: fake.handler(p, request))
        yield fake


@pytest.fixture
def fake_all():
    """ECOS and SDMX fakes behind one respx router (the World Bank fake is on ``sdmx.wb``)."""
    ecos, sdmx, wb = FakeEcos(), FakeSdmx(), FakeData360()
    sdmx.wb = wb
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://ecos.bok.or.kr/api/").mock(side_effect=ecos.handler)
        for provider, base in SDMX_BASES.items():
            router.get(url__startswith=base).mock(side_effect=lambda request, p=provider: sdmx.handler(p, request))
        router.route(url__startswith=DATA360_BASE).mock(side_effect=wb.handler)
        yield ecos, sdmx


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Validation ledger and revision snapshots go to a temp dir in every test."""
    monkeypatch.setenv("GESM_DATA_DIR", str(tmp_path / "gesm"))
