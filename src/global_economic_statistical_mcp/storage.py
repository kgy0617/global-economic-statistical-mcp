"""Local persistence for the validation layer: revision snapshots and the validation ledger.

Files live under ``$GESM_DATA_DIR`` (default ``$XDG_CACHE_HOME/global-economic-statistical-mcp``
or ``~/.cache/global-economic-statistical-mcp``). Set ``GESM_PERSIST=0`` to keep everything
in memory only (nothing is written), and ``GESM_LEDGER_RETENTION_DAYS`` to change how long
validation history is kept (default 90 days).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from global_economic_statistical_mcp.config import KST

_MEMORY_LIMIT = 1000  # series snapshots kept in memory per process
_MEMORY_RECORDS = 5000  # ledger records kept in memory per process
_DEFAULT_RETENTION_DAYS = 90


def _today():
    return datetime.now(KST).date()


def _retention_days() -> int:
    try:
        return max(1, int(os.getenv("GESM_LEDGER_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return _DEFAULT_RETENTION_DAYS


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
    """Validation results: a daily-partitioned history plus the latest state, with retention.

    ::

        $GESM_DATA_DIR/validation/
        ├── current/latest.json          latest result per provider pair and per series
        └── history/YYYY-MM-DD.jsonl     every record of that day (KST)

    History files older than ``GESM_LEDGER_RETENTION_DAYS`` (default 90) are deleted, so the
    ledger stays bounded; ``latest.json`` holds one entry per pair or series.
    """

    def __init__(self, root: Path | None = None, persist: bool | None = None, retention_days: int | None = None) -> None:
        self.root = (root or data_dir()) / "validation"
        self.persist = persistence_enabled() if persist is None else persist
        self.retention_days = retention_days if retention_days is not None else _retention_days()
        self._memory: list[dict[str, Any]] = []
        self._pruned_on: str | None = None

    @property
    def latest_path(self) -> Path:
        return self.root / "current" / "latest.json"

    def _history_path(self, day: str) -> Path:
        return self.root / "history" / f"{day}.jsonl"

    def append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self._memory.extend(records)
        del self._memory[:-_MEMORY_RECORDS]
        if not self.persist:
            return
        try:
            by_day: dict[str, list[dict[str, Any]]] = {}
            for record in records:
                by_day.setdefault(str(record.get("recorded_at", ""))[:10] or _today().isoformat(), []).append(record)
            for day, rows in by_day.items():
                path = self._history_path(day)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    for record in rows:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._update_latest(records)
            self._prune()
        except OSError:
            pass  # validation must never break data retrieval

    def _update_latest(self, records: list[dict[str, Any]]) -> None:
        latest = self.latest()
        for r in records:
            if r.get("kind") == "cross_validation":
                for provider, result in (r.get("by_provider") or {}).items():
                    key = f"{r.get('concept_id')}|{r.get('country')}|{r.get('reference')}~{provider}"
                    previous = latest["pairs"].get(key)
                    if previous and (previous["recorded_at"], previous["period"]) > (r["recorded_at"], r["period"]):
                        continue
                    latest["pairs"][key] = {
                        "recorded_at": r["recorded_at"],
                        "period": r["period"],
                        "validation_status": result.get("validation_status"),
                        "difference": result.get("difference"),
                        **({"explanation": result["explanation"]} if "explanation" in result else {}),
                    }
            elif r.get("kind") == "series_validation":
                latest["series"][r["series_id"]] = {"recorded_at": r["recorded_at"], "status": r["status"]}
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.latest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.latest_path)

    def _prune(self) -> None:
        today = _today().isoformat()
        if self._pruned_on == today:
            return
        self._pruned_on = today
        cutoff = (_today() - timedelta(days=self.retention_days)).isoformat()
        for path in (self.root / "history").glob("*.jsonl"):
            if path.stem < cutoff:
                path.unlink(missing_ok=True)

    def latest(self) -> dict[str, Any]:
        if self.persist and self.latest_path.exists():
            try:
                data = json.loads(self.latest_path.read_text(encoding="utf-8"))
                return {"pairs": data.get("pairs", {}), "series": data.get("series", {})}
            except (OSError, ValueError):
                pass
        return {"pairs": {}, "series": {}}

    def records(self, limit: int | None = None) -> list[dict[str, Any]]:
        """History records within the retention window (memory only when not persisting)."""
        if not self.persist:
            rows = list(self._memory)
        else:
            rows = []
            for path in sorted((self.root / "history").glob("*.jsonl")):
                try:
                    with path.open(encoding="utf-8") as f:
                        rows.extend(json.loads(line) for line in f if line.strip())
                except (OSError, ValueError):
                    continue
        return rows[-limit:] if limit else rows

    def summary(self) -> dict[str, Any]:
        """Per concept, country and provider pair: counts by status over the retention window, and the latest state."""
        stats: dict[str, Counter[str]] = {}
        for r in self.records():
            if r.get("kind") != "cross_validation":
                continue
            for provider, result in (r.get("by_provider") or {}).items():
                key = f"{r.get('concept_id')}|{r.get('country')}|{r.get('reference')}~{provider}"
                counter = stats.setdefault(key, Counter())
                counter[result.get("status", "unknown")] += 1
                counter[result.get("validation_status") or "UNCLASSIFIED"] += 1
        latest = self.latest()["pairs"]
        out = []
        for key, counts in sorted(stats.items()):
            concept, country, pair = key.split("|")
            total = sum(counts[k] for k in ("match", "within_tolerance", "mismatch", "unknown"))
            agree = counts.get("match", 0) + counts.get("within_tolerance", 0)
            out.append(
                {
                    "concept_id": concept,
                    "country": country,
                    "providers": pair.split("~"),
                    "comparisons": total,
                    "agreement_rate": round(agree / total, 4) if total else None,
                    "by_validation_status": {k: counts[k] for k in ("MATCH", "DIFFER", "UNRESOLVED") if counts[k]},
                    **({"latest": latest[key]} if key in latest else {}),
                }
            )
        return {
            "ledger": str(self.root) if self.persist else "(memory only)",
            "retention_days": self.retention_days,
            "pairs": out,
        }
