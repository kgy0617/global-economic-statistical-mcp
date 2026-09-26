"""Async HTTP client for the ECOS (Bank of Korea) Open API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from global_economic_statistical_mcp.config import (
    ECOS_API_KEY,
    ECOS_BASE_URL,
    ECOS_ERROR_MAP,
    ECOS_RESPONSE_TYPE,
    HTTP_MAX_RETRIES,
    HTTP_RETRY_BACKOFF_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    KEY_STATISTICS_CACHE_TTL_SECONDS,
    METADATA_CACHE_MAX_ENTRIES,
    METADATA_CACHE_TTL_SECONDS,
    SAMPLE_API_KEY,
    SAMPLE_KEY_MAX_COUNT,
)

# httpx logs every request URL at INFO level. ECOS puts the API key in the URL path,
# so those lines would leak the key into MCP client log files.
logging.getLogger("httpx").setLevel(logging.WARNING)

NO_DATA_CODE = "INFO-200"


class EcosApiError(Exception):
    """Raised when the ECOS API returns an error response or network fails."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        friendly = ECOS_ERROR_MAP.get(code, "")
        full_message = f"[{code}] {message}"
        if friendly:
            full_message += f" — {friendly}"
        super().__init__(full_message)


class _RetryableError(Exception):
    """Internal marker for failures worth retrying."""

    def __init__(self, error: EcosApiError) -> None:
        self.error = error


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _strip_numbering(name: str) -> str:
    """Drop the '1.2.3. ' outline prefix from an ECOS table name."""
    return re.sub(r"^[\d.]+\s*", "", name)


class EcosClient:
    """Async client for the ECOS Open API.

    All API responses are requested as JSON. The client handles URL construction,
    safe path encoding, response parsing, error detection, retries, sample-key
    clamping, metadata caching, and local table search.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = ECOS_BASE_URL,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        max_retries: int = HTTP_MAX_RETRIES,
        retry_backoff: float = HTTP_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.api_key = (api_key or "").strip() or ECOS_API_KEY
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._http = httpx.AsyncClient(timeout=timeout)
        self._tables_cache: list[dict[str, Any]] | None = None
        self._tables_generated_at: str | None = None
        self._response_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def is_sample_key(self) -> bool:
        return self.api_key == SAMPLE_API_KEY

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    # ── Internal helpers ──────────────────────────────────────────────

    def _build_url(self, *path_segments: str) -> str:
        """Build a full ECOS API URL from path segments.

        URL pattern:
        {base}/{ServiceName}/{ApiKey}/{Type}/{Language}/{StartCount}/{EndCount}/...
        Each segment is sanitized (slashes replaced with space to prevent BOK WAF 503)
        and properly URL-encoded.
        """
        clean_segments = [
            urllib.parse.quote(str(seg).replace("/", " ").strip(), safe="")
            for seg in path_segments
        ]
        return f"{self.base_url}/" + "/".join(clean_segments)

    def _redact(self, text: str) -> str:
        """Remove the API key from text that may be shown to users or logged."""
        if self.api_key and not self.is_sample_key:
            text = text.replace(self.api_key, "***")
            text = text.replace(urllib.parse.quote(self.api_key, safe=""), "***")
        return text

    @staticmethod
    def _extract_result(
        data: dict[str, Any], service_name: str
    ) -> tuple[int, list[dict[str, Any]]]:
        """Extract total_count and row list from ECOS JSON responses.

        ECOS responses are wrapped like:
        {
            "ServiceName": {
                "list_total_count": N,
                "row": [...]
            }
        }
        A "no data" result (INFO-200) is returned as an empty row list, not an error.
        """
        # Check for top-level error response
        if "RESULT" in data:
            result = data["RESULT"]
            code = result.get("CODE", "UNKNOWN")
            if code == NO_DATA_CODE:
                return 0, []
            raise EcosApiError(code=code, message=result.get("MESSAGE", "Unknown error"))

        service_data = data.get(service_name)
        if service_data is None:
            raise EcosApiError(
                code="PARSE_ERROR",
                message=f"예상치 못한 응답 구조입니다. '{service_name}' 키가 누락되었습니다.",
            )

        # Check for nested error
        if "RESULT" in service_data:
            result_code = service_data["RESULT"].get("CODE", "")
            if result_code == NO_DATA_CODE:
                return 0, []
            if result_code and not result_code.startswith("INFO-000"):
                raise EcosApiError(
                    code=result_code,
                    message=service_data["RESULT"].get("MESSAGE", ""),
                )

        total_count = int(service_data.get("list_total_count", 0))
        rows = service_data.get("row", [])
        return total_count, rows

    async def _get_json_once(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url)
        except httpx.TimeoutException as e:
            raise _RetryableError(
                EcosApiError(
                    code="TIMEOUT",
                    message=f"ECOS API 서버 응답 시간 초과 ({self.timeout:g}초)",
                )
            ) from e
        except httpx.RequestError as e:
            raise _RetryableError(
                EcosApiError(
                    code="NETWORK_ERROR",
                    message=f"ECOS 서버와 통신할 수 없습니다: {type(e).__name__}",
                )
            ) from e

        if response.status_code >= 400:
            error = EcosApiError(
                code=f"HTTP_{response.status_code}",
                message=f"ECOS 서버 HTTP 에러: {response.status_code} {response.reason_phrase}",
            )
            if response.status_code >= 500 or response.status_code == 429:
                raise _RetryableError(error)
            raise error

        try:
            return response.json()
        except ValueError as e:
            raise EcosApiError(
                code="PARSE_ERROR",
                message="ECOS 응답을 JSON으로 해석할 수 없습니다.",
            ) from e

    async def _get_json(self, url: str) -> dict[str, Any]:
        """GET a URL, retrying transient failures with exponential backoff."""
        for attempt in range(self.max_retries + 1):
            try:
                return await self._get_json_once(url)
            except _RetryableError as retryable:
                if attempt >= self.max_retries:
                    raise retryable.error from retryable.__cause__
                await asyncio.sleep(self.retry_backoff * (2**attempt))
        raise AssertionError("unreachable")

    async def _request(
        self,
        service_name: str,
        language: str,
        start_count: int,
        end_count: int,
        *extra_params: str,
        cache_ttl: float | None = None,
    ) -> dict[str, Any]:
        """Make a request to the ECOS API and return parsed result dict."""
        start_count = max(1, start_count)
        end_count = max(start_count, end_count)
        clamped = False

        # sample key only allows up to 10 items per call
        if self.is_sample_key and (end_count - start_count + 1 > SAMPLE_KEY_MAX_COUNT):
            end_count = start_count + SAMPLE_KEY_MAX_COUNT - 1
            clamped = True

        url = self._build_url(
            service_name,
            self.api_key,
            ECOS_RESPONSE_TYPE,
            language,
            str(start_count),
            str(end_count),
            *extra_params,
        )

        cached = self._response_cache.get(url) if cache_ttl else None
        if cached and cached[0] > time.monotonic():
            data = cached[1]
        else:
            try:
                data = await self._get_json(url)
            except EcosApiError as e:
                raise EcosApiError(e.code, self._redact(e.message)) from None
            if cache_ttl:
                if len(self._response_cache) >= METADATA_CACHE_MAX_ENTRIES:
                    # dicts keep insertion order: evict the oldest entry
                    self._response_cache.pop(next(iter(self._response_cache)))
                self._response_cache[url] = (time.monotonic() + cache_ttl, data)

        total_count, rows = self._extract_result(data, service_name)

        result: dict[str, Any] = {
            "total_count": total_count,
            "count": len(rows),
            "start_count": start_count,
            "end_count": end_count,
            "has_more": start_count + len(rows) - 1 < total_count,
            "rows": rows,
        }
        notes: list[str] = []
        if total_count == 0:
            notes.append("조건에 맞는 데이터가 없습니다. 기간, 주기, 항목코드를 확인하세요.")
        if clamped and result["has_more"]:
            notes.append(
                "API 인증키가 'sample'이므로 1회 최대 조회 한도(10건)로 자동 제한되었습니다. "
                "전체 조회를 원하시면 ECOS에서 무료 인증키를 발급받아 ECOS_API_KEY에 설정하세요."
            )
        if notes:
            result["note"] = " ".join(notes)
        return result

    # ── Table Cache Helper ───────────────────────────────────────────

    def _load_tables_cache(self) -> list[dict[str, Any]]:
        """Load the pre-indexed statistical table metadata.

        tables.json is either a bare list of StatisticTableList rows or
        {"generated_at": ..., "tables": [...]} as written by scripts/update_tables.py.
        """
        if self._tables_cache is None:
            cache_file = Path(__file__).parent / "catalog" / "data" / "tables.json"
            data: Any = []
            if cache_file.exists():
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            if isinstance(data, dict):
                self._tables_generated_at = data.get("generated_at")
                data = data.get("tables", [])
            self._tables_cache = data
        return self._tables_cache

    @property
    def tables_generated_at(self) -> str | None:
        self._load_tables_cache()
        return self._tables_generated_at

    # ── Public API methods ────────────────────────────────────────────

    async def get_key_statistics(
        self,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 100,
    ) -> dict[str, Any]:
        """Fetch the top 100 key economic indicators."""
        return await self._request(
            "KeyStatisticList", language, start_count, end_count
        )

    async def list_statistic_tables(
        self,
        stat_code: str | None = None,
        searchable_only: bool = False,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 100,
    ) -> dict[str, Any]:
        """List available statistical tables from the live API.

        Used by scripts/update_tables.py to rebuild the local index. The MCP tools
        browse the local index instead (see browse_statistic_tables).
        """
        extra = [stat_code] if stat_code else []
        result = await self._request(
            "StatisticTableList", language, start_count, end_count, *extra
        )
        if searchable_only:
            filtered_rows = [r for r in result["rows"] if r.get("SRCH_YN") == "Y"]
            result["rows"] = filtered_rows
            result["count"] = len(filtered_rows)
        return result

    def search_statistic_tables(
        self,
        keyword: str,
        searchable_only: bool = True,
        limit: int = 30,
        parent_code: str | None = None,
    ) -> dict[str, Any]:
        """Search statistical tables by name using pre-indexed table metadata.

        Whitespace-separated words must all appear (spaces inside names are ignored,
        so '소비자 물가' matches '소비자물가지수'). Results are ranked: exact code/name,
        then name prefix, then contiguous substring, then scattered word matches.

        Args:
            keyword: Search query (e.g., '물가', '금리', '환율', 'GDP').
            searchable_only: If True, returns only tables where SRCH_YN == 'Y'.
            limit: Maximum number of results to return.
            parent_code: If given, only tables under this node of the table tree.
        """
        tables = self._load_tables_cache()
        allowed = self._descendant_codes(parent_code.strip()) if parent_code else None
        phrase = _normalize_text(keyword)
        tokens = [_normalize_text(t) for t in keyword.split() if t.strip()]

        scored: list[tuple[int, int, dict[str, Any]]] = []
        if tokens:
            for position, t in enumerate(tables):
                if searchable_only and t.get("SRCH_YN") != "Y":
                    continue
                if allowed is not None and t.get("STAT_CODE") not in allowed:
                    continue
                name = _normalize_text(_strip_numbering(t.get("STAT_NAME") or ""))
                code = (t.get("STAT_CODE") or "").lower()
                if not all(tok in name or tok in code for tok in tokens):
                    continue
                if phrase in (code, name):
                    score = 0
                elif name.startswith(phrase):
                    score = 1
                elif phrase in name or phrase in code:
                    score = 2
                else:
                    score = 3
                scored.append((score, position, t))

        scored.sort(key=lambda item: (item[0], item[1]))
        limit = max(1, limit)
        return {
            "query": keyword,
            "total_matches": len(scored),
            "count": min(len(scored), limit),
            "searchable_only": searchable_only,
            "index_generated_at": self.tables_generated_at,
            "rows": [t for _, _, t in scored[:limit]],
        }

    def _descendant_codes(self, root_code: str) -> set[str]:
        """All STAT_CODEs below root_code in the local table tree."""
        children: dict[str, list[str]] = {}
        for t in self._load_tables_cache():
            children.setdefault(t.get("P_STAT_CODE") or "", []).append(t.get("STAT_CODE") or "")
        found: set[str] = set()
        stack = [root_code]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found

    def table_info(self, stat_code: str) -> dict[str, Any] | None:
        """Look up a table (or category) in the local index."""
        code = stat_code.strip()
        return next((t for t in self._load_tables_cache() if t.get("STAT_CODE") == code), None)

    async def _fetch_all_pages(
        self,
        service_name: str,
        language: str,
        *extra: str,
        max_rows: int,
        max_calls: int,
        cache_ttl: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch every page of a list service (concurrently after the first page).

        Returns (rows, total_count). Stops at max_rows rows or max_calls requests; when
        capped, pages are taken from both ends so that later item groups (listed last
        by ECOS) are still represented.
        """
        page = SAMPLE_KEY_MAX_COUNT if self.is_sample_key else 1000
        first = await self._request(service_name, language, 1, page, *extra, cache_ttl=cache_ttl)
        total = first["total_count"]
        last_row = min(total, max_rows)
        starts = list(range(page + 1, last_row + 1, page))
        budget = max(0, max_calls - 1)
        if len(starts) > budget:
            head = budget // 2
            starts = starts[:head] + starts[len(starts) - (budget - head) :]
        pages = await asyncio.gather(
            *(
                self._request(
                    service_name, language, start, min(start + page - 1, last_row), *extra, cache_ttl=cache_ttl
                )
                for start in starts
            )
        )
        rows = list(first["rows"])
        for result in pages:
            rows.extend(result["rows"])
        return rows, total

    async def list_all_statistic_items(
        self,
        stat_code: str,
        language: str = "kr",
        max_rows: int = 10000,
    ) -> dict[str, Any]:
        """All StatisticItemList rows for a table (capped at ~10 calls for the sample key)."""
        rows, total = await self._fetch_all_pages(
            "StatisticItemList",
            language,
            stat_code.strip(),
            max_rows=max_rows,
            max_calls=10 if self.is_sample_key else 20,
            cache_ttl=METADATA_CACHE_TTL_SECONDS,
        )
        return {"total_count": total, "rows": rows, "complete": len(rows) >= total}

    async def get_all_statistic_meta(self, data_name: str, language: str = "kr") -> dict[str, Any]:
        """All StatisticMeta rows for a dataset name (section headers and their texts)."""
        rows, total = await self._fetch_all_pages(
            "StatisticMeta",
            language,
            data_name,
            max_rows=500,
            max_calls=10,
            cache_ttl=METADATA_CACHE_TTL_SECONDS,
        )
        return {"total_count": total, "rows": rows, "complete": len(rows) >= total}

    async def get_all_key_statistics(self, language: str = "kr") -> dict[str, Any]:
        """The full 100대 주요 경제지표 list (cached for an hour; values change daily)."""
        rows, total = await self._fetch_all_pages(
            "KeyStatisticList",
            language,
            max_rows=200,
            max_calls=20,
            cache_ttl=KEY_STATISTICS_CACHE_TTL_SECONDS,
        )
        return {"total_count": total, "rows": rows, "complete": len(rows) >= total}

    def browse_statistic_tables(self, parent_code: str | None = None) -> dict[str, Any]:
        """List the direct children of a table-tree node from the local index.

        Args:
            parent_code: Parent STAT_CODE. None returns the top-level categories.
        """
        tables = self._load_tables_cache()
        parent = parent_code.strip() if parent_code and parent_code.strip() else "*"
        children = [t for t in tables if t.get("P_STAT_CODE") == parent]
        node = next((t for t in tables if t.get("STAT_CODE") == parent), None)
        return {
            "parent": node,
            "total_matches": len(children),
            "count": len(children),
            "index_generated_at": self.tables_generated_at,
            "rows": children,
        }

    async def search_statistic_word(
        self,
        word: str,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 10,
    ) -> dict[str, Any]:
        """Search the statistical terminology dictionary."""
        return await self._request(
            "StatisticWord",
            language,
            start_count,
            end_count,
            word,
            cache_ttl=METADATA_CACHE_TTL_SECONDS,
        )

    async def list_statistic_items(
        self,
        stat_code: str,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 100,
    ) -> dict[str, Any]:
        """List sub-items for a specific statistical table."""
        return await self._request(
            "StatisticItemList",
            language,
            start_count,
            end_count,
            stat_code,
            cache_ttl=METADATA_CACHE_TTL_SECONDS,
        )

    async def search_statistics(
        self,
        stat_code: str,
        cycle: str,
        start_date: str,
        end_date: str,
        item_code1: str | None = None,
        item_code2: str | None = None,
        item_code3: str | None = None,
        item_code4: str | None = None,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 1000,
        prefer_latest: bool = True,
    ) -> dict[str, Any]:
        """Search for time-series statistical data.

        ECOS returns rows oldest-first (all items of a period together). When the
        result does not fit in one page and prefer_latest is True, the last page is
        fetched instead so callers see the most recent periods rather than the oldest.
        """
        extra: list[str] = [stat_code, cycle, start_date, end_date]

        # Item codes: must be provided in order; use "?" wildcard for intermediate skips
        item_codes = [item_code1, item_code2, item_code3, item_code4]
        # Trim trailing None values
        while item_codes and item_codes[-1] is None:
            item_codes.pop()
        for code in item_codes:
            extra.append(code if code is not None else "?")

        result = await self._request(
            "StatisticSearch", language, start_count, end_count, *extra
        )
        if not result["has_more"]:
            return result

        page_size = result["end_count"] - result["start_count"] + 1
        total = result["total_count"]
        if prefer_latest and result["start_count"] == 1:
            tail_start = max(1, total - page_size + 1)
            result = await self._request(
                "StatisticSearch", language, tail_start, total, *extra
            )
            truncation_note = (
                f"전체 {total}건 중 가장 최근 {result['count']}건만 반환했습니다. "
                "전체가 필요하면 기간을 줄이거나 prefer_latest=False와 start_count/end_count로 나눠 조회하세요."
            )
        else:
            truncation_note = (
                f"전체 {total}건 중 {result['start_count']}~{result['start_count'] + result['count'] - 1}번째만 "
                "반환했습니다. 나머지는 start_count/end_count를 조정해 조회하세요."
            )
        result["truncated"] = True
        result["note"] = " ".join(n for n in (truncation_note, result.get("note")) if n)
        return result

    async def get_statistic_meta(
        self,
        data_name: str,
        language: str = "kr",
        start_count: int = 1,
        end_count: int = 100,
    ) -> dict[str, Any]:
        """Get metadata for a statistical dataset."""
        return await self._request(
            "StatisticMeta",
            language,
            start_count,
            end_count,
            data_name,
            cache_ttl=METADATA_CACHE_TTL_SECONDS,
        )
