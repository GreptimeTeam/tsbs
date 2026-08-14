#!/usr/bin/env python3
"""Compare compatible GreptimeDB TSBS query benchmark runs."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


SCRIPT_PATH = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPT_PATH.parents[3] / "lib"))

from tsbs_benchmark import build_summary, new_run_dir, sha256_file, utc_now  # noqa: E402


SCHEMA_VERSION = 1


class ComparisonError(RuntimeError):
    """Raised when benchmark runs cannot be compared safely."""


def read_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ComparisonError(f"missing run manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ComparisonError(f"invalid run manifest {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("kind") != "greptimedb-run":
        raise ComparisonError(f"unsupported GreptimeDB run manifest: {path}")
    return value


def comparable_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    dataset = manifest.get("dataset") or {}
    query_set = manifest.get("query_set") or {}
    return {
        "database": manifest.get("database"),
        "dataset": {key: dataset.get(key) for key in ("dataset_id", "spec", "format", "bytes", "sha256")},
        "query_set": {key: query_set.get(key) for key in ("query_set_id", "manifest_sha256", "spec")},
    }


def run_for_comparison(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    manifest = read_manifest(run_dir)
    target = manifest.get("target") or {}
    if target.get("mode") != "managed" or not target.get("version") or not target.get("binary_sha256"):
        raise ComparisonError(f"comparison requires a managed run with recorded GreptimeDB version identity: {run_dir}")
    summary = build_summary(run_dir, manifest)
    if summary["failures"]:
        raise ComparisonError(f"comparison run has failures: {run_dir}")
    expected_counts = ((manifest.get("query_set") or {}).get("spec") or {}).get("query_counts")
    if not isinstance(expected_counts, dict) or not expected_counts:
        raise ComparisonError(f"comparison run has no complete query-set identity: {run_dir}")
    queries = {query["query_type"]: query for query in summary["queries"]}
    if set(queries) != set(expected_counts):
        raise ComparisonError(f"comparison run does not have completed results for every query type: {run_dir}")
    for query_type, expected_count in expected_counts.items():
        query = queries[query_type]
        if query["query_count"] != expected_count * query["repetitions"]:
            raise ComparisonError(f"comparison run has an incomplete result count for {query_type}: {run_dir}")
    identity = comparable_identity(manifest)
    dataset = identity["dataset"]
    query_set = identity["query_set"]
    if (
        not isinstance(identity["database"], str)
        or not identity["database"]
        or not isinstance(dataset.get("dataset_id"), str)
        or not isinstance(dataset.get("spec"), dict)
        or not isinstance(dataset.get("sha256"), str)
        or dataset.get("bytes") is None
        or not isinstance(query_set.get("query_set_id"), str)
        or not isinstance(query_set.get("manifest_sha256"), str)
        or not isinstance(query_set.get("spec"), dict)
    ):
        raise ComparisonError(f"comparison run has incomplete dataset or query-set identity: {run_dir}")
    return {
        "path": str(run_dir),
        "run_id": summary["run_id"],
        "database_id": target.get("database_id"),
        "version": target["version"],
        "binary_sha256": target["binary_sha256"],
        "manifest_sha256": sha256_file(run_dir / "manifest.json"),
        "identity": identity,
        "queries": queries,
    }


def run_identity(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run[key] for key in ("path", "run_id", "database_id", "version", "binary_sha256", "manifest_sha256")}


def compare_candidate(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate["identity"] != baseline["identity"]:
        raise ComparisonError(
            f"candidate run workload does not match baseline: {candidate['path']}"
        )
    comparisons: list[dict[str, Any]] = []
    for query_type in sorted(baseline["queries"]):
        base = baseline["queries"][query_type]
        other = candidate["queries"][query_type]
        if (base["repetitions"], base["query_count"]) != (other["repetitions"], other["query_count"]):
            raise ComparisonError(f"candidate query repetitions or count differ for {query_type}: {candidate['path']}")
        baseline_ms = base["weighted_mean_milliseconds"]
        candidate_ms = other["weighted_mean_milliseconds"]
        delta_ms = candidate_ms - baseline_ms
        if baseline_ms == 0:
            ratio = 1.0 if candidate_ms == 0 else None
            delta_percent = 0.0 if candidate_ms == 0 else None
        else:
            ratio = candidate_ms / baseline_ms
            delta_percent = (ratio - 1.0) * 100.0
        classification = "regressed" if delta_ms > 0 else "improved" if delta_ms < 0 else "unchanged"
        comparisons.append({
            "query_type": query_type,
            "repetitions": base["repetitions"],
            "query_count": base["query_count"],
            "baseline_milliseconds": baseline_ms,
            "candidate_milliseconds": candidate_ms,
            "delta_milliseconds": delta_ms,
            "delta_percent": delta_percent,
            "latency_ratio": ratio,
            "classification": classification,
            "baseline_logs": [str(Path(baseline["path"]) / run["log"]) for run in base["runs"]],
            "candidate_logs": [str(Path(candidate["path"]) / run["log"]) for run in other["runs"]],
        })
    counts = {name: sum(item["classification"] == name for item in comparisons) for name in ("improved", "unchanged", "regressed")}
    regressions = [item for item in comparisons if item["classification"] == "regressed"]
    largest = max(regressions, key=lambda item: item["delta_percent"] if item["delta_percent"] is not None else float("inf"), default=None)
    return {
        **run_identity(candidate),
        "counts": counts,
        "largest_regression": None if largest is None else {
            key: largest[key] for key in ("query_type", "delta_milliseconds", "delta_percent", "latency_ratio")
        },
        "queries": comparisons,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    baseline = summary["baseline"]
    lines = [
        f"# GreptimeDB version comparison: {summary['comparison_id']}", "",
        f"- Baseline: `{baseline['version']}` (`{baseline['run_id']}`)",
        f"- Baseline run: `{baseline['path']}`", "",
    ]
    for candidate in summary["candidates"]:
        lines.extend([
            f"## Candidate `{candidate['version']}` (`{candidate['run_id']}`)", "",
            f"- Candidate run: `{candidate['path']}`",
            f"- Improved: {candidate['counts']['improved']}; unchanged: {candidate['counts']['unchanged']}; regressed: {candidate['counts']['regressed']}", "",
            "| Query type | Baseline (ms) | Candidate (ms) | Delta (ms) | Delta (%) | Latency ratio | Result |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ])
        for query in candidate["queries"]:
            percent = "-" if query["delta_percent"] is None else f"{query['delta_percent']:.2f}%"
            ratio = "-" if query["latency_ratio"] is None else f"{query['latency_ratio']:.3f}x"
            lines.append(
                f"| `{query['query_type']}` | {query['baseline_milliseconds']:.3f} | "
                f"{query['candidate_milliseconds']:.3f} | {query['delta_milliseconds']:.3f} | "
                f"{percent} | {ratio} | {query['classification']} |"
            )
        if candidate["largest_regression"]:
            largest = candidate["largest_regression"]
            percent = "undefined" if largest["delta_percent"] is None else f"{largest['delta_percent']:.2f}%"
            lines.extend(["", f"Largest regression: `{largest['query_type']}` ({percent})."])
        lines.append("")
    return "\n".join(lines)


def create_comparison(baseline_path: Path, candidate_paths: Sequence[Path], comparison_root: Path) -> Path:
    if not candidate_paths:
        raise ComparisonError("provide at least one --candidate-run")
    baseline = run_for_comparison(baseline_path)
    candidates = [compare_candidate(baseline, run_for_comparison(path)) for path in candidate_paths]
    destination = new_run_dir(comparison_root.expanduser().resolve())
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        summary = {
            "schema_version": SCHEMA_VERSION,
            "kind": "greptimedb-comparison",
            "comparison_id": destination.name,
            "created_at": utc_now(),
            "database": baseline["identity"]["database"],
            "dataset": baseline["identity"]["dataset"],
            "query_set": baseline["identity"]["query_set"],
            "baseline": run_identity(baseline),
            "candidates": candidates,
        }
        (temporary / "manifest.json").write_text(json.dumps({key: value for key, value in summary.items() if key != "candidates"} | {"candidate_runs": [run_identity(candidate) for candidate in candidates]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temporary / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (temporary / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
