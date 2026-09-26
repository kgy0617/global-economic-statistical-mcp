"""Provider interface: every data source turns a SeriesRequest into CanonicalSeries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from global_economic_statistical_mcp.model import CanonicalSeries


class ProviderError(Exception):
    """A provider failed in a way the caller should report (never contains credentials)."""

    def __init__(self, provider: str, code: str, message: str) -> None:
        self.provider = provider
        self.code = code
        self.message = message
        super().__init__(f"[{provider} {code}] {message}")


@dataclass
class SeriesRequest:
    provider: str
    dataflow: str  # ECOS stat code, or SDMX "AGENCY:ID(VERSION)"
    key: str  # ECOS: dot-joined item codes ("0000001.0000100"); SDMX: series key
    freq: str
    start: str  # canonical period
    end: str  # canonical period
    language: str = "kr"
    start_count: int = 1  # ECOS paging
    end_count: int = 1000
    prefer_latest: bool = True


class Provider(Protocol):
    id: str
    name: str

    async def fetch(self, request: SeriesRequest) -> list[CanonicalSeries]: ...

    async def close(self) -> None: ...
