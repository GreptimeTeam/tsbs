"""Shared, database-neutral helpers for repository TSBS benchmark skills."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, TextIO, TypeVar


RESULT_FORMAT_VERSION = "0.2"
LEGACY_RESULT_FORMAT_VERSION = "0.1"

QUERY_COUNTS_MANUAL = {
    "cpu-max-all-1": 100,
    "cpu-max-all-8": 100,
    "double-groupby-1": 50,
    "double-groupby-5": 50,
    "double-groupby-all": 50,
    "groupby-orderby-limit": 50,
    "high-cpu-1": 100,
    "high-cpu-all": 50,
    "lastpoint": 10,
    "single-groupby-1-1-1": 100,
    "single-groupby-1-1-12": 100,
    "single-groupby-1-8-1": 100,
    "single-groupby-5-1-1": 100,
    "single-groupby-5-1-12": 100,
    "single-groupby-5-8-1": 100,
}
QUERY_TYPES = tuple(QUERY_COUNTS_MANUAL)
PROFILES = {
    "manual": {
        "start": "2023-06-11T00:00:00Z",
        "end": "2023-06-14T00:00:00Z",
        "scale": 4000,
        "seed": 123,
        "log_interval": "10s",
        "load_workers": 6,
        "query_workers": 1,
        "batch_size": 3000,
        "query_counts": QUERY_COUNTS_MANUAL,
    },
    "smoke": {
        "start": "2023-06-11T00:00:00Z",
        "end": "2023-06-12T00:00:00Z",
        "scale": 10,
        "seed": 123,
        "log_interval": "10s",
        "load_workers": 2,
        "query_workers": 1,
        "batch_size": 3000,
        "query_counts": {query_type: 10 for query_type in QUERY_TYPES},
    },
}

METRIC_RE = re.compile(
    r"loaded\s+(?P<count>\d+)\s+metrics\s+in\s+(?P<seconds>[0-9.]+)sec.*?"
    r"mean rate\s+(?P<rate>[0-9.]+)\s+metrics/sec"
)
ROW_RE = re.compile(
    r"loaded\s+(?P<count>\d+)\s+rows\s+in\s+(?P<seconds>[0-9.]+)sec.*?"
    r"mean rate\s+(?P<rate>[0-9.]+)\s+rows/sec"
)
QUERY_RE = re.compile(
    r"all queries\s*:\s*\n\s*"
    r"min:.*?mean:\s*(?P<mean>[0-9.]+)ms,.*?count:\s*(?P<count>\d+)",
    re.DOTALL,
)


class SummaryError(ValueError):
    """Raised when a completed TSBS result or legacy log cannot be parsed."""


ErrorType = TypeVar("ErrorType", bound=Exception)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def write_streams(text: str, *destinations: TextIO) -> None:
    """Write text to every destination and make it immediately observable."""
    for destination in destinations:
        destination.write(text)
        destination.flush()


def tee_stream(source: Iterable[str], *destinations: TextIO) -> None:
    """Copy a text stream and flush every complete line to each destination."""
    for line in source:
        write_streams(line, *destinations)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def read_json(path: Path, error_type: type[ErrorType]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise error_type(f"missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise error_type(f"invalid JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise error_type(f"manifest must be an object: {path}")
    return value


def new_run_dir(run_root: Path) -> Path:
    run_root.mkdir(parents=True, exist_ok=True)
    base = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = run_root / base
    suffix = 1
    while candidate.exists():
        candidate = run_root / f"{base}-{suffix:02d}"
        suffix += 1
    return candidate


def add_one_second(timestamp: str) -> str:
    parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (parsed + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")


def build_workload(
    args: argparse.Namespace,
    base: dict[str, Any] | None = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if base is not None and not args.profile:
        workload = json.loads(json.dumps(base))
    else:
        workload = json.loads(json.dumps(PROFILES[args.profile or "manual"]))
        if defaults:
            workload.update(defaults)
    for attr in (
        "start",
        "end",
        "scale",
        "seed",
        "log_interval",
        "load_workers",
        "query_workers",
        "batch_size",
    ):
        value = getattr(args, attr, None)
        if value is not None:
            workload[attr] = value
    selected = sorted(set(args.query_type or workload["query_counts"]))
    count_override = getattr(args, "queries", None)
    workload["query_counts"] = {
        query_type: count_override
        if count_override is not None
        else workload["query_counts"][query_type]
        for query_type in selected
    }
    return workload


def relative(run_dir: Path, path: Path) -> str:
    return str(path.relative_to(run_dir))


def query_file_path(query_dir: Path, query_type: str) -> Path:
    return query_dir / "queries" / f"{query_type}.dat"


def dataset_binding(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": dataset["dataset_id"],
        "spec": dataset["spec"],
        "format": dataset["format"],
        "bytes": dataset["bytes"],
        "sha256": dataset["sha256"],
    }


def next_attempt(
    events: Sequence[dict[str, Any]], query_type: str | None = None
) -> int:
    matching = [
        event
        for event in events
        if query_type is None or event.get("query_type") == query_type
    ]
    return max((int(event["attempt"]) for event in matching), default=0) + 1


@contextlib.contextmanager
def lock_directory(
    path: Path, error_type: type[ErrorType], description: str
) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise error_type(f"{description} is locked: {path}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SummaryError(f"result field {field} must be an object")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SummaryError(f"result field {field} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise SummaryError(
            f"result field {field} must be a non-negative finite number"
        )
    return result


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryError(f"result field {field} must be a non-negative integer")
    return value


def parse_load_result(result: dict[str, Any]) -> dict[str, Any]:
    totals = _mapping(result.get("Totals"), "Totals")
    metrics = _count(totals.get("metricCount"), "Totals.metricCount")
    metric_rate = _number(totals.get("metricRate"), "Totals.metricRate")
    rows = _count(totals.get("rowCount"), "Totals.rowCount")
    row_rate = _number(totals.get("rowRate"), "Totals.rowRate")
    parsed: dict[str, Any] = {
        "metrics": metrics,
        "duration_seconds": _number(result.get("DurationMillis"), "DurationMillis")
        / 1000.0,
        "metrics_per_second": metric_rate,
    }
    if rows > 0:
        parsed.update({"rows": rows, "rows_per_second": row_rate})
    return parsed


def parse_query_result(result: dict[str, Any]) -> dict[str, Any]:
    totals = _mapping(result.get("Totals"), "Totals")
    overall_stats = _mapping(totals.get("overallStats"), "Totals.overallStats")
    all_queries = _mapping(
        overall_stats.get("all_queries"), "Totals.overallStats.all_queries"
    )
    return {
        "mean_milliseconds": _number(
            all_queries.get("meanMilliseconds"),
            "Totals.overallStats.all_queries.meanMilliseconds",
        ),
        "count": _count(
            all_queries.get("count"), "Totals.overallStats.all_queries.count"
        ),
    }


def parse_load_log(text: str) -> dict[str, Any]:
    metric_matches = list(METRIC_RE.finditer(text))
    if not metric_matches:
        raise SummaryError("load log has no final metric summary")
    metric = metric_matches[-1]
    result: dict[str, Any] = {
        "metrics": int(metric.group("count")),
        "duration_seconds": float(metric.group("seconds")),
        "metrics_per_second": float(metric.group("rate")),
    }
    row_matches = list(ROW_RE.finditer(text))
    if row_matches:
        row = row_matches[-1]
        result.update(
            {
                "rows": int(row.group("count")),
                "rows_per_second": float(row.group("rate")),
            }
        )
    return result


def parse_query_log(text: str) -> dict[str, Any]:
    marker = text.rfind("Run complete after")
    if marker < 0:
        raise SummaryError("query log has no final run marker")
    matches = list(QUERY_RE.finditer(text[marker:]))
    if not matches:
        raise SummaryError("query log has no final all-queries summary")
    match = matches[-1]
    return {
        "mean_milliseconds": float(match.group("mean")),
        "count": int(match.group("count")),
    }


def read_log(run_dir: Path, relative_path: str) -> str:
    return (run_dir / relative_path).read_text(encoding="utf-8", errors="replace")


def _read_event_result(
    run_dir: Path, event: dict[str, Any]
) -> dict[str, Any] | None:
    relative_path = event.get("results")
    if not relative_path:
        return None
    path = run_dir / relative_path
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SummaryError(f"malformed result JSON {relative_path}: {exc}") from exc
    result = _mapping(result, str(relative_path))
    version = result.get("ResultFormatVersion")
    if version == LEGACY_RESULT_FORMAT_VERSION:
        return None
    if version != RESULT_FORMAT_VERSION:
        raise SummaryError(
            f"unsupported result format version {version!r} in {relative_path}"
        )
    return result


def _parse_event(
    run_dir: Path,
    event: dict[str, Any],
    result_parser: Callable[[dict[str, Any]], dict[str, Any]],
    log_parser: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    result = _read_event_result(run_dir, event)
    if result is not None:
        return result_parser(result)
    return log_parser(read_log(run_dir, event["log"]))


def build_summary(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    ingestion_runs: list[dict[str, Any]] = []
    query_runs: list[dict[str, Any]] = []

    for event in manifest.get("events", {}).get("loads", []):
        if event.get("status") == "reused":
            continue
        base = {
            "attempt": event["attempt"],
            "database": event["database"],
            "mode": event["database_mode"],
            "log": event["log"],
        }
        if event.get("status") != "completed":
            failures.append(
                {
                    "stage": "load",
                    "log": event["log"],
                    "reason": event.get("status", "failed"),
                }
            )
            continue
        try:
            base.update(
                _parse_event(run_dir, event, parse_load_result, parse_load_log)
            )
            ingestion_runs.append(base)
        except (OSError, SummaryError) as exc:
            failures.append(
                {"stage": "load", "log": event["log"], "reason": str(exc)}
            )

    for event in manifest.get("events", {}).get("queries", []):
        base = {
            "query_type": event["query_type"],
            "attempt": event["attempt"],
            "database": event["database"],
            "log": event["log"],
        }
        if event.get("status") != "completed":
            failures.append(
                {
                    "stage": "query",
                    "log": event["log"],
                    "reason": event.get("status", "failed"),
                }
            )
            continue
        try:
            base.update(
                _parse_event(run_dir, event, parse_query_result, parse_query_log)
            )
            query_runs.append(base)
        except (OSError, SummaryError) as exc:
            failures.append(
                {"stage": "query", "log": event["log"], "reason": str(exc)}
            )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in query_runs:
        key = (run["database"], run["query_type"])
        grouped.setdefault(key, []).append(run)

    queries: list[dict[str, Any]] = []
    for database, query_type in sorted(grouped):
        runs = grouped[(database, query_type)]
        count = sum(run["count"] for run in runs)
        weighted = sum(run["mean_milliseconds"] * run["count"] for run in runs)
        queries.append(
            {
                "database": database,
                "query_type": query_type,
                "repetitions": len(runs),
                "query_count": count,
                "weighted_mean_milliseconds": weighted / count if count else 0.0,
                "runs": runs,
            }
        )

    return {
        "run_id": manifest.get("run_id", run_dir.name),
        "profile": manifest.get("profile"),
        "database": manifest.get("database"),
        "target": manifest.get("target"),
        "dataset": manifest.get("dataset"),
        "query_set": manifest.get("query_set"),
        "workload": manifest.get("workload", {}),
        "ingestion_runs": ingestion_runs,
        "queries": queries,
        "failures": failures,
    }
