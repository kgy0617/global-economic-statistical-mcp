"""Local persistence for the validation layer: revision snapshots and the validation ledger.

Files live under ``$GESM_DATA_DIR`` (default ``$XDG_CACHE_HOME/global-economic-statistical-mcp``
or ``~/.cache/global-economic-statistical-mcp``). Set ``GESM_PERSIST=0`` to keep everything
in memory only (nothing is written).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

_MEMORY_LIMIT = 1000  # series snapshots kept in memory per process


def data_dir() -> Path:
    explicit = os.getenv("GESM_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    base = os.getenv("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "global-economic-statistical-mcp"


def persistence_enabled() -> bool:
    return os.getenv("GESM_PERSIST", "1").strip().lower() not in ("0", "false", "no", "off")


class RevisionStore:
    """Last seen values per series, used to detect revisions between retrievals."""

    def __init__(self, root: Path | None = None, persist: bool | None = None) -> None:
        self.root = (root or data_dir()) / "revisions"
        self.persist = persistence_enabled() if persist is None else persist
        self._memory: dict[str, dict[str, Any]] = {}

    def _path(self, series_id: str) -> Path:
        return self.root / f"{hashlib.sha1(series_id.encode()).hexdigest()}.json"

    def load(self, series_id: str) -> dict[str, Any] | None:
        if series_id in self._memory:
            return self._memory[series_id]
        if self.persist:
            path = self._path(series_id)
            if path.exists():
                try:
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
                self._memory[series_id] = snapshot
                return snapshot
        return None

    def save(self, series_id: str, values: dict[str, float | None], retrieved_at: str) -> None:
        previous = self.load(series_id) or {"values": {}}
        merged = {**previous.get("values", {}), **values}
        snapshot = {"series_id": series_id, "retrieved_at": retrieved_at, "values": merged}
        self._memory.pop(series_id, None)
        if len(self._memory) >= _MEMORY_LIMIT:
            self._memory.pop(next(iter(self._memory)))  # oldest; the disk copy remains
        self._memory[series_id] = snapshot
        if self.persist:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                self._path(series_id).write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass  # validation must never break data retrieval


class ValidationLedger:
    """Append-only JSONL record of validation results — the basis of reliability statistics."""

    def __init__(self, root: Path | None = None, persist: bool | None = None) -> None:
        self.path = (root or data_dir()) / "validation_ledger.jsonl"
        self.persist = persistence_enabled() if persist is None else persist
        self._memory: list[dict[str, Any]] = []

    def append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self._memory.extend(records)
        if self.persist:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    for record in records:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def records(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self.persist and self.path.exists():
            try:
                with self.path.open(encoding="utf-8") as f:
                    rows = [json.loads(line) for line in f if line.strip()]
            except (OSError, ValueError):
                rows = []
        else:
            rows = list(self._memory)
        return rows[-limit:] if limit else rows

    def summary(self) -> dict[str, Any]:
        """Agreement statistics per concept, country and provider pair (cross-validation records)."""
        stats: dict[str, Counter[str]] = {}
        for r in self.records():
            if r.get("kind") != "cross_validation":
                continue
            for provider, result in (r.get("by_provider") or {}).items():
                key = f"{r.get('concept_id')}|{r.get('country')}|{r.get('reference')}~{provider}"
                stats.setdefault(key, Counter())[result.get("status", "unknown")] += 1
        out = []
        for key, counts in sorted(stats.items()):
            concept, country, pair = key.split("|")
            total = sum(counts.values())
            agree = counts.get("match", 0) + counts.get("within_tolerance", 0)
            out.append(
                {
                    "concept_id": concept,
                    "country": country,
                    "providers": pair.split("~"),
                    "comparisons": total,
                    "agreement_rate": round(agree / total, 4) if total else None,
                    **dict(counts),
                }
            )
        return {"ledger": str(self.path) if self.persist else "(memory only)", "pairs": out}
