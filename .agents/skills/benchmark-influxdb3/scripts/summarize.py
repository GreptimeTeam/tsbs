#!/usr/bin/env python3
"""Parse TSBS results and write InfluxDB 3 benchmark summaries."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT_PATH.parents[3] / "lib"))

from tsbs_benchmark import (  # noqa: E402
    SummaryError,
    build_summary as build_common_summary,
    parse_load_log,
    parse_load_result,
    parse_query_log,
    parse_query_result,
    read_log,
)


MAX_SERVER_DIAGNOSTIC_SAMPLES = 20
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
PANIC_RE = re.compile(r"\bpanic(?:ked)?\b|fatal runtime error", re.IGNORECASE)
FATAL_RE = re.compile(r"\bfatal\b", re.IGNORECASE)
ERROR_RE = re.compile(r"(?:^|\s)ERROR(?:\s|$)", re.IGNORECASE)
WARNING_RE = re.compile(
    r"(?:^|\s)WARN(?:ING)?(?:\s|$)|<jemalloc>:", re.IGNORECASE
)


def server_diagnostics(
    run_dir: Path,
    manifest: dict[str, Any],
    failures: list[dict[str, str]],
) -> dict[str, Any]:
    counts = {"warning": 0, "error": 0, "fatal": 0, "panic": 0}
    samples: list[dict[str, str]] = []
    attempts: list[dict[str, Any]] = []
    failing_statuses = {
        "starting",
        "startup_failed",
        "startup_timeout",
        "unexpected_exit",
        "forced_shutdown",
    }
    for event in manifest.get("events", {}).get("servers", []):
        log = event.get("log", "")
        attempt = {
            key: event.get(key)
            for key in (
                "attempt",
                "log",
                "status",
                "started_at",
                "ready_at",
                "finished_at",
                "exit_code",
                "forced_shutdown",
                "unexpected_exit",
            )
        }
        attempts.append(attempt)
        status = event.get("status", "unknown")
        if (
            status in failing_statuses
            or event.get("unexpected_exit")
            or event.get("forced_shutdown")
        ):
            failures.append({"stage": "server", "log": log, "reason": status})
        try:
            lines = read_log(run_dir, log).splitlines()
        except OSError as exc:
            failures.append({"stage": "server", "log": log, "reason": str(exc)})
            continue
        fatal_or_panic = False
        for raw_line in lines:
            line = EMAIL_RE.sub("<redacted-email>", raw_line.strip())
            severity = None
            if PANIC_RE.search(line):
                severity = "panic"
            elif FATAL_RE.search(line):
                severity = "fatal"
            elif ERROR_RE.search(line):
                severity = "error"
            elif WARNING_RE.search(line):
                severity = "warning"
            if severity is None:
                continue
            counts[severity] += 1
            if len(samples) < MAX_SERVER_DIAGNOSTIC_SAMPLES:
                samples.append(
                    {"severity": severity, "log": log, "message": line}
                )
            fatal_or_panic = fatal_or_panic or severity in ("fatal", "panic")
        if fatal_or_panic:
            failures.append(
                {
                    "stage": "server",
                    "log": log,
                    "reason": "server log contains fatal or panic diagnostics",
                }
            )
    return {
        **{f"{name}_count": value for name, value in counts.items()},
        "samples": samples,
        "attempts": attempts,
    }


def build_summary(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    summary = build_common_summary(run_dir, manifest)
    summary["server_diagnostics"] = server_diagnostics(
        run_dir, manifest, summary["failures"]
    )
    return summary


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
        lines.append(
            f"- Durable WAL acknowledgement: `{not target.get('no_sync', False)}`"
        )
        lines.append(
            f"- Partial batch acceptance: `{target.get('accept_partial', False)}`"
        )
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
            rows_per_second = (
                f"{run['rows_per_second']:.2f}"
                if "rows_per_second" in run
                else "-"
            )
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
        f"Warnings: {diagnostics.get('warning_count', 0)}; "
        f"errors: {diagnostics.get('error_count', 0)}; "
        f"fatals: {diagnostics.get('fatal_count', 0)}; "
        f"panics: {diagnostics.get('panic_count', 0)}."
    )
    if diagnostics.get("samples"):
        lines.extend(
            ["", "| Severity | Log | Sample |", "| --- | --- | --- |"]
        )
        for sample in diagnostics["samples"]:
            message = sample["message"].replace("|", "\\|")
            lines.append(
                f"| {sample['severity']} | `{sample['log']}` | {message} |"
            )

    if summary["failures"]:
        lines.extend(
            [
                "",
                "## Failures",
                "",
                "| Stage | Log | Reason |",
                "| --- | --- | --- |",
            ]
        )
        for failure in summary["failures"]:
            reason = failure["reason"].replace("|", "\\|")
            lines.append(f"| {failure['stage']} | `{failure['log']}` | {reason} |")
    lines.append("")
    return "\n".join(lines)


def write_summary(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    summary = build_summary(run_dir, manifest)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
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
