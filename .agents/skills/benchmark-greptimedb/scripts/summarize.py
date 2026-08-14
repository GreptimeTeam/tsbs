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
    build_summary,
    parse_load_log,
    parse_load_result,
    parse_query_log,
    parse_query_result,
)


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
