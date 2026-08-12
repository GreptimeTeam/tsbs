#!/usr/bin/env python3
"""Parse TSBS results and write InfluxDB 3 benchmark summaries."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Callable


RESULT_FORMAT_VERSION = "0.2"
LEGACY_RESULT_FORMAT_VERSION = "0.1"
MAX_SERVER_DIAGNOSTIC_SAMPLES = 20
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
PANIC_RE = re.compile(r"\bpanic(?:ked)?\b|fatal runtime error", re.IGNORECASE)
FATAL_RE = re.compile(r"\bfatal\b", re.IGNORECASE)
ERROR_RE = re.compile(r"(?:^|\s)ERROR(?:\s|$)", re.IGNORECASE)
WARNING_RE = re.compile(r"(?:^|\s)WARN(?:ING)?(?:\s|$)|<jemalloc>:", re.IGNORECASE)


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


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SummaryError(f"result field {field} must be an object")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SummaryError(f"result field {field} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise SummaryError(f"result field {field} must be a non-negative finite number")
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
        "duration_seconds": _number(result.get("DurationMillis"), "DurationMillis") / 1000.0,
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


def _read_log(run_dir: Path, relative_path: str) -> str:
    return (run_dir / relative_path).read_text(encoding="utf-8", errors="replace")


def _read_event_result(run_dir: Path, event: dict[str, Any]) -> dict[str, Any] | None:
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
        raise SummaryError(f"unsupported result format version {version!r} in {relative_path}")
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
    return log_parser(_read_log(run_dir, event["log"]))


def server_diagnostics(run_dir: Path, manifest: dict[str, Any], failures: list[dict[str, str]]) -> dict[str, Any]:
    counts = {"warning": 0, "error": 0, "fatal": 0, "panic": 0}
    samples: list[dict[str, str]] = []
    attempts: list[dict[str, Any]] = []
    failing_statuses = {"starting", "startup_failed", "startup_timeout", "unexpected_exit", "forced_shutdown"}
    for event in manifest.get("events", {}).get("servers", []):
        log = event.get("log", "")
        attempt = {key: event.get(key) for key in ("attempt", "log", "status", "started_at", "ready_at", "finished_at", "exit_code", "forced_shutdown", "unexpected_exit")}
        attempts.append(attempt)
        status = event.get("status", "unknown")
        if status in failing_statuses or event.get("unexpected_exit") or event.get("forced_shutdown"):
            failures.append({"stage": "server", "log": log, "reason": status})
        try:
            lines = _read_log(run_dir, log).splitlines()
        except OSError as exc:
            failures.append({"stage": "server", "log": log, "reason": str(exc)})
            continue
        fatal_or_panic = False
        for raw_line in lines:
            line = EMAIL_RE.sub("<redacted-email>", raw_line.strip())
            severity = None
            if PANIC_RE.search(line): severity = "panic"
            elif FATAL_RE.search(line): severity = "fatal"
            elif ERROR_RE.search(line): severity = "error"
            elif WARNING_RE.search(line): severity = "warning"
            if severity is None:
                continue
            counts[severity] += 1
            if len(samples) < MAX_SERVER_DIAGNOSTIC_SAMPLES:
                samples.append({"severity": severity, "log": log, "message": line})
            fatal_or_panic = fatal_or_panic or severity in ("fatal", "panic")
        if fatal_or_panic:
            failures.append({"stage": "server", "log": log, "reason": "server log contains fatal or panic diagnostics"})
    return {**{f"{name}_count": value for name, value in counts.items()}, "samples": samples, "attempts": attempts}


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
                {"stage": "load", "log": event["log"], "reason": event.get("status", "failed")}
            )
            continue
        try:
            base.update(_parse_event(run_dir, event, parse_load_result, parse_load_log))
            ingestion_runs.append(base)
        except (OSError, SummaryError) as exc:
            failures.append({"stage": "load", "log": event["log"], "reason": str(exc)})

    for event in manifest.get("events", {}).get("queries", []):
        base = {
            "query_type": event["query_type"],
            "attempt": event["attempt"],
            "database": event["database"],
            "log": event["log"],
        }
        if event.get("status") != "completed":
            failures.append(
                {"stage": "query", "log": event["log"], "reason": event.get("status", "failed")}
            )
            continue
        try:
            base.update(_parse_event(run_dir, event, parse_query_result, parse_query_log))
            query_runs.append(base)
        except (OSError, SummaryError) as exc:
            failures.append({"stage": "query", "log": event["log"], "reason": str(exc)})

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

    diagnostics = server_diagnostics(run_dir, manifest, failures)
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
        "server_diagnostics": diagnostics,
        "failures": failures,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# InfluxDB 3 TSBS benchmark: {summary['run_id']}",
        "",
        f"- Profile: `{summary.get('profile')}`",
        f"- Current database: `{summary.get('database')}`",
    ]
    target = summary.get("target")
    if target:
        target_name = target.get("database_id") or ",".join(target.get("urls", []))
        lines.append(f"- Benchmark target: `{target.get('mode')}:{target_name}`")
        lines.append(f"- Edition: `{target.get('edition')}`")
        if target.get("version"):
            lines.append(f"- Version: `{target.get('version')}`")
        lines.append(f"- Durable WAL acknowledgement: `{not target.get('no_sync', False)}`")
        lines.append(f"- Partial batch acceptance: `{target.get('accept_partial', False)}`")
    dataset = summary.get("dataset")
    if dataset:
        lines.extend(
            [
                f"- Dataset: `{dataset.get('dataset_id')}`",
                f"- Data format: `{dataset.get('format')}`",
                f"- Data SHA-256: `{dataset.get('sha256')}`",
                f"- Data path: `{dataset.get('data_path')}`",
            ]
        )
    query_set = summary.get("query_set")
    if query_set:
        lines.extend(
            [
                f"- Query set: `{query_set.get('query_set_id')}`",
                f"- Query-set manifest SHA-256: `{query_set.get('manifest_sha256')}`",
            ]
        )
    lines.extend(["", "## Ingestion", ""])
    if summary["ingestion_runs"]:
        lines.extend(
            [
                "| Database | Attempt | Mode | Metrics | Metrics/s | Rows | Rows/s | Log |",
                "| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for run in summary["ingestion_runs"]:
            rows_per_second = f"{run['rows_per_second']:.2f}" if "rows_per_second" in run else "-"
            lines.append(
                f"| `{run['database']}` | {run['attempt']} | {run['mode']} | {run['metrics']} | "
                f"{run['metrics_per_second']:.2f} | {run.get('rows', '-')} | {rows_per_second} | "
                f"`{run['log']}` |"
            )
    else:
        lines.append("No completed ingestion runs.")

    lines.extend(["", "## Queries", ""])
    if summary["queries"]:
        lines.extend(
            [
                "| Database | Query type | Repetitions | Query count | Weighted mean (ms) |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
        )
        for query in summary["queries"]:
            lines.append(
                f"| `{query['database']}` | `{query['query_type']}` | {query['repetitions']} | "
                f"{query['query_count']} | "
                f"{query['weighted_mean_milliseconds']:.3f} |"
            )
    else:
        lines.append("No completed query runs.")

    diagnostics = summary.get("server_diagnostics", {})
    lines.extend(["", "## Server diagnostics", ""])
    lines.append(
        f"Warnings: {diagnostics.get('warning_count', 0)}; errors: {diagnostics.get('error_count', 0)}; "
        f"fatals: {diagnostics.get('fatal_count', 0)}; panics: {diagnostics.get('panic_count', 0)}."
    )
    if diagnostics.get("samples"):
        lines.extend(["", "| Severity | Log | Sample |", "| --- | --- | --- |"])
        for sample in diagnostics["samples"]:
            message = sample["message"].replace("|", "\\|")
            lines.append(f"| {sample['severity']} | `{sample['log']}` | {message} |")

    if summary["failures"]:
        lines.extend(["", "## Failures", "", "| Stage | Log | Reason |", "| --- | --- | --- |"])
        for failure in summary["failures"]:
            reason = failure["reason"].replace("|", "\\|")
            lines.append(f"| {failure['stage']} | `{failure['log']}` | {reason} |")
    lines.append("")
    return "\n".join(lines)


def write_summary(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    summary = build_summary(run_dir, manifest)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (run_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = write_summary(run_dir, manifest)
    print(run_dir / "summary.md")
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
