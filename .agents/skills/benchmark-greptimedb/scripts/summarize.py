#!/usr/bin/env python3
"""Parse TSBS results and write GreptimeDB benchmark summaries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT_PATH.parents[3] / "lib"))

from tsbs_benchmark import (  # noqa: E402
    SummaryError,
    build_summary as build_benchmark_summary,
    parse_load_log,
    parse_load_result,
    parse_query_log,
    parse_query_result,
)


def _validate_analysis_result(run_dir: Path, relative_path: str, phase: str, query_index: int) -> None:
    path = run_dir / relative_path
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SummaryError(f"malformed analysis result JSON {relative_path}: {exc}") from exc
    if not isinstance(result, dict):
        raise SummaryError(f"analysis result must be an object: {relative_path}")
    required = {"phase", "query_index", "sql", "executed_sql", "response"}
    if not required.issubset(result):
        raise SummaryError(f"analysis result is missing required fields: {relative_path}")
    if result["phase"] != phase or result["query_index"] != query_index:
        raise SummaryError(f"analysis result phase or query index mismatch: {relative_path}")
    if not isinstance(result["sql"], str) or not isinstance(result["executed_sql"], str):
        raise SummaryError(f"analysis result SQL fields must be strings: {relative_path}")
    if result["executed_sql"] != "EXPLAIN ANALYZE VERBOSE " + result["sql"]:
        raise SummaryError(f"analysis result executed SQL mismatch: {relative_path}")
    if not isinstance(result["response"], dict):
        raise SummaryError(f"analysis response must be an object: {relative_path}")


def build_summary(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    summary = build_benchmark_summary(run_dir, manifest)
    analyses: list[dict[str, Any]] = []
    for event in manifest.get("events", {}).get("analyses", []):
        base = {
            "query_type": event["query_type"],
            "attempt": event["attempt"],
            "database": event["database"],
            "hot_runs": event["hot_runs"],
            "cold_query_index": event["cold_query_index"],
            "hot_query_indices": event["hot_query_indices"],
            "result_dir": event["result_dir"],
            "cold_result": event["cold_result"],
            "hot_results": event["hot_results"],
            "metrics": event["metrics"],
            "log": event["log"],
            "server_log": event["server_log"],
        }
        if event.get("status") != "completed":
            failure_log = event["log"] if (run_dir / event["log"]).is_file() else event["server_log"]
            summary["failures"].append(
                {"stage": "analyze", "log": failure_log, "reason": event.get("reason", event.get("status", "failed"))}
            )
            continue
        try:
            _validate_analysis_result(run_dir, event["cold_result"], "cold", event["cold_query_index"])
            if len(event["hot_results"]) != len(event["hot_query_indices"]):
                raise SummaryError(f"analysis hot result count mismatch: {event['result_dir']}")
            for result_path, query_index in zip(event["hot_results"], event["hot_query_indices"]):
                _validate_analysis_result(run_dir, result_path, "hot", query_index)
            for artifact in (event["metrics"], event["log"], event["server_log"]):
                if not (run_dir / artifact).is_file():
                    raise SummaryError(f"missing analysis artifact: {artifact}")
            analyses.append(base)
        except (OSError, SummaryError) as exc:
            summary["failures"].append({"stage": "analyze", "log": event["log"], "reason": str(exc)})
    summary["analyses"] = analyses
    return summary


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# GreptimeDB TSBS benchmark: {summary['run_id']}",
        "",
        f"- Profile: `{summary.get('profile')}`",
        f"- Current database: `{summary.get('database')}`",
    ]
    target = summary.get("target")
    if target:
        target_name = target.get("database_id") or target.get("endpoint")
        lines.append(f"- Benchmark target: `{target.get('mode')}:{target_name}`")
        if target.get("version"):
            label = "Runtime GreptimeDB version" if target.get("version_override") else "GreptimeDB version"
            lines.append(f"- {label}: `{target.get('version')}`")
        if target.get("binary_sha256"):
            label = "Runtime GreptimeDB binary SHA-256" if target.get("version_override") else "GreptimeDB binary SHA-256"
            lines.append(f"- {label}: `{target.get('binary_sha256')}`")
        if target.get("version_override"):
            lines.append(f"- Workspace-bound GreptimeDB version: `{target.get('workspace_version')}`")
            lines.append(f"- Workspace-bound binary SHA-256: `{target.get('workspace_binary_sha256')}`")
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

    lines.extend(["", "## Analysis", ""])
    if summary.get("analyses"):
        lines.extend(
            [
                "| Database | Query type | Attempt | Cold query | Hot queries | Results | Runner log | Server log |",
                "| --- | --- | ---: | ---: | --- | --- | --- | --- |",
            ]
        )
        for analysis in summary["analyses"]:
            hot_queries = ", ".join(str(index) for index in analysis["hot_query_indices"])
            lines.append(
                f"| `{analysis['database']}` | `{analysis['query_type']}` | {analysis['attempt']} | "
                f"{analysis['cold_query_index']} | {hot_queries} | `{analysis['result_dir']}` | "
                f"`{analysis['log']}` | `{analysis['server_log']}` |"
            )
    else:
        lines.append("No completed analysis runs.")

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
