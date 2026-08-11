#!/usr/bin/env python3
"""Run GreptimeDB TSBS benchmarks using shared, validated artifacts."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Sequence

from summarize import write_summary


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
BENCHMARK_ROOT = REPO_ROOT / ".benchmarks"
DEFAULT_RUN_ROOT = BENCHMARK_ROOT / "greptimedb" / "runs"
DEFAULT_QUERY_ROOT = BENCHMARK_ROOT / "queries"
DEFAULT_DATABASE_ROOT = BENCHMARK_ROOT / "greptimedb" / "databases"
DEFAULT_DATASET_ROOT = BENCHMARK_ROOT / "datasets"
DATASET_RUNNER = REPO_ROOT / ".agents" / "skills" / "generate-tsbs-data" / "scripts" / "generate.py"
DEFAULT_DATABASE = "benchmark"
SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DATA_WORKLOAD_OPTIONS = ("start", "end", "scale", "seed", "log_interval")

QUERY_COUNTS_MANUAL = {
    "cpu-max-all-1": 100, "cpu-max-all-8": 100, "double-groupby-1": 50,
    "double-groupby-5": 50, "double-groupby-all": 50, "groupby-orderby-limit": 50,
    "high-cpu-1": 100, "high-cpu-all": 50, "lastpoint": 10,
    "single-groupby-1-1-1": 100, "single-groupby-1-1-12": 100,
    "single-groupby-1-8-1": 100, "single-groupby-5-1-1": 100,
    "single-groupby-5-1-12": 100, "single-groupby-5-8-1": 100,
}
QUERY_TYPES = tuple(QUERY_COUNTS_MANUAL)
PROFILES = {
    "manual": {
        "start": "2023-06-11T00:00:00Z", "end": "2023-06-14T00:00:00Z",
        "scale": 4000, "seed": 123, "log_interval": "10s", "load_workers": 6,
        "query_workers": 1, "batch_size": 3000, "query_counts": QUERY_COUNTS_MANUAL,
    },
    "smoke": {
        "start": "2023-06-11T00:00:00Z", "end": "2023-06-12T00:00:00Z",
        "scale": 10, "seed": 123, "log_interval": "10s", "load_workers": 2,
        "query_workers": 1, "batch_size": 3000,
        "query_counts": {query_type: 10 for query_type in QUERY_TYPES},
    },
}
BINARIES = {"queries": "tsbs_generate_queries", "load": "tsbs_load_greptime", "query": "tsbs_run_queries_influx"}
BUILT_THIS_PROCESS: set[str] = set()


class BenchmarkError(RuntimeError):
    """Raised for an actionable benchmark failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkError(f"missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"invalid JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"manifest must be an object: {path}")
    return value


def new_run_dir(run_root: Path = DEFAULT_RUN_ROOT) -> Path:
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


def validate_run_manifest(manifest: dict[str, Any], path: Path) -> None:
    required = {"schema_version", "kind", "run_id", "created_at", "profile", "workload", "events"}
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "greptimedb-run":
        raise BenchmarkError(f"unsupported run manifest schema: {path}")
    if not required.issubset(manifest) or not isinstance(manifest["workload"], dict):
        raise BenchmarkError(f"malformed run manifest: {path}")
    events = manifest["events"]
    if not isinstance(events, dict) or not isinstance(events.get("loads"), list) or not isinstance(events.get("queries"), list):
        raise BenchmarkError(f"malformed run events: {path}")
    workload = manifest["workload"]
    workload_fields = ("start", "end", "scale", "seed", "log_interval", "load_workers", "query_workers", "batch_size", "query_counts")
    if not all(field in workload for field in workload_fields) or not isinstance(workload["query_counts"], dict):
        raise BenchmarkError(f"malformed run workload: {path}")


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    save_json(run_dir / "manifest.json", manifest)


def resolve_database(args: argparse.Namespace) -> None:
    if not hasattr(args, "database") or args.database is not None:
        return
    args.database = DEFAULT_DATABASE
    if args.run_dir and (args.run_dir.resolve() / "manifest.json").exists():
        manifest = read_json(args.run_dir.resolve() / "manifest.json")
        validate_run_manifest(manifest, args.run_dir.resolve() / "manifest.json")
        args.database = manifest.get("database", DEFAULT_DATABASE)


def prepare_run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    run_root = (args.run_root or DEFAULT_RUN_ROOT).expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve() if args.run_dir else new_run_dir(run_root)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "results").mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        validate_run_manifest(manifest, manifest_path)
        workload_options = ("profile", "start", "end", "scale", "seed", "log_interval", "load_workers", "query_workers", "batch_size", "queries", "query_type")
        if any(getattr(args, name, None) is not None for name in workload_options):
            requested = build_workload(args, manifest["workload"])
            if requested != manifest["workload"]:
                raise BenchmarkError("run workload is immutable; create a new run for different settings")
    else:
        profile = args.profile or "manual"
        manifest = {
            "schema_version": SCHEMA_VERSION, "kind": "greptimedb-run", "run_id": run_dir.name,
            "created_at": utc_now(), "profile": profile, "database": getattr(args, "database", DEFAULT_DATABASE),
            "workload": build_workload(args), "events": {"loads": [], "queries": []},
        }
    if hasattr(args, "database") and manifest.get("database") != args.database and manifest_path.exists():
        raise BenchmarkError("--database conflicts with the database pinned by this run")
    save_manifest(run_dir, manifest)
    return run_dir, manifest


def build_workload(args: argparse.Namespace, base: dict[str, Any] | None = None) -> dict[str, Any]:
    if base is not None and not args.profile:
        workload = json.loads(json.dumps(base))
    else:
        workload = json.loads(json.dumps(PROFILES[args.profile or "manual"]))
    for attr in ("start", "end", "scale", "seed", "log_interval", "load_workers", "query_workers", "batch_size"):
        value = getattr(args, attr, None)
        if value is not None:
            workload[attr] = value
    selected = sorted(set(args.query_type or workload["query_counts"]))
    count_override = getattr(args, "queries", None)
    workload["query_counts"] = {query_type: count_override if count_override is not None else workload["query_counts"][query_type] for query_type in selected}
    return workload


def relative(run_dir: Path, path: Path) -> str:
    return str(path.relative_to(run_dir))


def display_command(command: Sequence[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in command)


def run_tee(command: Sequence[str], log_path: Path, *, stdout_path: Path | None = None, append: bool = False) -> None:
    mode = "a" if append else "w"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open(mode, encoding="utf-8") as log:
        header = f"$ {display_command(command)}\n"
        log.write(header); log.flush(); print(header, end="")
        if stdout_path is None:
            process = subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line); log.write(line)
            process.stdout.close(); return_code = process.wait()
        else:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_output = stdout_path.with_name(stdout_path.name + ".tmp")
            with temporary_output.open("wb") as output:
                process = subprocess.Popen(command, cwd=REPO_ROOT, stdout=output, stderr=subprocess.PIPE, text=True, bufsize=1)
                assert process.stderr is not None
                for line in process.stderr:
                    sys.stdout.write(line); log.write(line)
                process.stderr.close(); return_code = process.wait()
        if return_code:
            if stdout_path is not None:
                temporary_output.unlink(missing_ok=True)
            raise BenchmarkError(f"command failed with exit code {return_code}; see {log_path}")
        if stdout_path is not None:
            os.replace(temporary_output, stdout_path)


def binary_needs_build(run_dir: Path, name: str, target: Path, rebuild: bool) -> bool:
    marker = run_dir / "results" / f"built-{name}"
    return name not in BUILT_THIS_PROCESS and (rebuild or not (target.exists() and marker.exists()))


def ensure_binaries(run_dir: Path, stages: Sequence[str], rebuild: bool) -> None:
    for stage in stages:
        name = BINARIES[stage]; target = REPO_ROOT / "bin" / name
        marker = run_dir / "results" / f"built-{name}"
        if not binary_needs_build(run_dir, name, target, rebuild):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        run_tee(["go", "build", "-o", str(target), f"./cmd/{name}"], run_dir / "logs" / "build.log", append=True)
        marker.write_text(utc_now() + "\n", encoding="utf-8"); BUILT_THIS_PROCESS.add(name)


def dataset_selection_args(args: argparse.Namespace, manifest: dict[str, Any]) -> list[str]:
    pinned = manifest.get("dataset")
    if pinned:
        pinned_path = Path(pinned["dataset_path"]).resolve()
        if args.dataset_path and args.dataset_path.resolve() != pinned_path:
            raise BenchmarkError("--dataset-path conflicts with the dataset pinned by this run")
        if args.dataset_id and args.dataset_id != pinned["dataset_id"]:
            raise BenchmarkError("--dataset-id conflicts with the dataset pinned by this run")
        return ["--dataset-path", str(pinned_path)]
    if args.dataset_path:
        return ["--dataset-path", str(args.dataset_path.resolve())]
    result: list[str] = []
    if args.dataset_root:
        result.extend(["--dataset-root", str(args.dataset_root.resolve())])
    if args.dataset_id:
        result.extend(["--dataset-id", args.dataset_id])
    return result


def prepare_dataset(args: argparse.Namespace, run_dir: Path, manifest: dict[str, Any], *, materialize: bool) -> dict[str, Any]:
    workload = manifest["workload"]
    result_path = run_dir / "results" / ("dataset.json" if materialize else "logical-dataset.json")
    command = [sys.executable, str(DATASET_RUNNER), "generate" if materialize else "prepare"]
    if materialize:
        command.extend(["--format", "influx"])
    command.extend(["--use-case", "cpu-only", "--result-file", str(result_path), *dataset_selection_args(args, manifest)])
    if not manifest.get("dataset"):
        command.extend(["--seed", str(workload["seed"]), "--scale", str(workload["scale"]), "--start", workload["start"], "--end", workload["end"], "--log-interval", workload["log_interval"]])
    if materialize and args.regenerate:
        command.append("--regenerate")
    if materialize and args.rebuild:
        command.append("--rebuild")
    run_tee(command, run_dir / "logs" / ("generate-data.log" if materialize else "prepare-dataset.log"))
    dataset = read_json(result_path)
    if dataset["spec"].get("use_case") != "cpu-only":
        raise BenchmarkError("GreptimeDB benchmark requires a cpu-only dataset")
    pinned = manifest.get("dataset")
    if pinned and pinned.get("dataset_id") != dataset.get("dataset_id"):
        raise BenchmarkError("prepared dataset differs from the dataset pinned by this run")
    for name in DATA_WORKLOAD_OPTIONS:
        workload[name] = dataset["spec"][name]
    manifest["dataset"] = dataset
    save_manifest(run_dir, manifest)
    return dataset


def generate_data(args: argparse.Namespace, run_dir: Path, manifest: dict[str, Any]) -> Path:
    dataset = prepare_dataset(args, run_dir, manifest, materialize=True)
    return Path(dataset["data_path"])


def query_set_spec(dataset: dict[str, Any], workload: dict[str, Any]) -> dict[str, Any]:
    counts = {name: int(count) for name, count in sorted(workload["query_counts"].items())}
    return {
        "dataset": {"dataset_id": dataset["dataset_id"], "spec": dataset["spec"]},
        "format": "greptime", "use_case": "devops", "seed": workload["seed"],
        "scale": workload["scale"], "timestamp_start": workload["start"],
        "timestamp_end": add_one_second(workload["end"]), "query_counts": counts,
    }


def query_set_id(spec: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json({"schema_version": SCHEMA_VERSION, "spec": spec}).encode()).hexdigest()
    return f"greptime-{digest[:16]}"


def query_set_path(query_root: Path, dataset_id: str, set_id: str) -> Path:
    return query_root / dataset_id / "greptime" / set_id


def query_file_path(query_dir: Path, query_type: str) -> Path:
    return query_dir / "queries" / f"{query_type}.dat"


def validate_query_set(query_dir: Path, expected_spec: dict[str, Any]) -> dict[str, Any]:
    manifest_path = query_dir / "manifest.json"; manifest = read_json(manifest_path)
    required = {"schema_version", "kind", "query_set_id", "created_at", "spec", "generator", "files"}
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "greptimedb-query-set" or not required.issubset(manifest):
        raise BenchmarkError(f"malformed query-set manifest: {manifest_path}")
    if not isinstance(manifest["spec"], dict) or manifest["spec"] != expected_spec or manifest["query_set_id"] != query_set_id(expected_spec):
        raise BenchmarkError(f"query-set specification mismatch: {query_dir}")
    expected_types = set(expected_spec["query_counts"]); files = manifest["files"]
    if not isinstance(files, dict) or set(files) != expected_types:
        raise BenchmarkError(f"query-set membership mismatch: {query_dir}")
    actual_names = {path.stem for path in (query_dir / "queries").glob("*.dat")}
    if actual_names != expected_types:
        raise BenchmarkError(f"query-set artifacts do not match membership: {query_dir}")
    for query_type, metadata in files.items():
        if not isinstance(metadata, dict) or metadata.get("path") != f"queries/{query_type}.dat" or not isinstance(metadata.get("bytes"), int) or not isinstance(metadata.get("sha256"), str):
            raise BenchmarkError(f"malformed query artifact metadata: {query_dir}")
        path = query_file_path(query_dir, query_type)
        if not path.is_file() or path.stat().st_size != metadata.get("bytes") or sha256_file(path) != metadata.get("sha256"):
            raise BenchmarkError(f"query artifact checksum mismatch: {path}")
    return manifest


def git_revision() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def generate_queries(args: argparse.Namespace, run_dir: Path, manifest: dict[str, Any]) -> Path:
    dataset = manifest.get("dataset") or prepare_dataset(args, run_dir, manifest, materialize=False)
    spec = query_set_spec(dataset, manifest["workload"]); set_id = query_set_id(spec)
    root = (args.query_root or DEFAULT_QUERY_ROOT).expanduser().resolve()
    destination = query_set_path(root, dataset["dataset_id"], set_id)
    if destination.exists():
        set_manifest = validate_query_set(destination, spec); reused = True
    else:
        ensure_binaries(run_dir, ["queries"], args.rebuild)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{set_id}-", dir=destination.parent))
        try:
            files: dict[str, dict[str, Any]] = {}
            for query_type, count in spec["query_counts"].items():
                output = query_file_path(temporary, query_type)
                command = [str(REPO_ROOT / "bin" / BINARIES["queries"]), f"--use-case={spec['use_case']}", f"--seed={spec['seed']}", f"--scale={spec['scale']}", f"--timestamp-start={spec['timestamp_start']}", f"--timestamp-end={spec['timestamp_end']}", f"--queries={count}", f"--query-type={query_type}", f"--format={spec['format']}"]
                run_tee(command, run_dir / "logs" / f"generate-query-{query_type}.log", stdout_path=output)
                files[query_type] = {"path": f"queries/{query_type}.dat", "bytes": output.stat().st_size, "sha256": sha256_file(output)}
            binary = REPO_ROOT / "bin" / BINARIES["queries"]
            set_manifest = {"schema_version": SCHEMA_VERSION, "kind": "greptimedb-query-set", "query_set_id": set_id, "created_at": utc_now(), "spec": spec, "generator": {"binary": "bin/tsbs_generate_queries", "binary_sha256": sha256_file(binary), "git_revision": git_revision()}, "files": files}
            save_json(temporary / "manifest.json", set_manifest)
            try:
                os.replace(temporary, destination)
            except OSError:
                if not destination.exists():
                    raise
                validate_query_set(destination, spec)
            reused = False
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    set_manifest = validate_query_set(destination, spec)
    manifest_checksum = sha256_file(destination / "manifest.json")
    pinned = {"query_set_id": set_id, "query_set_path": str(destination), "manifest_sha256": manifest_checksum, "spec": spec, "reused": reused}
    existing = manifest.get("query_set")
    if existing and {k: existing.get(k) for k in ("query_set_id", "manifest_sha256", "spec")} != {k: pinned[k] for k in ("query_set_id", "manifest_sha256", "spec")}:
        raise BenchmarkError("query set conflicts with the query set pinned by this run")
    manifest["query_set"] = pinned; save_manifest(run_dir, manifest)
    return destination


def database_mode_args(mode: str, database: str, confirmation: str | None) -> list[str]:
    if mode == "create": return ["--do-create-db=true", "--do-abort-on-exist=true"]
    if mode == "reuse": return ["--do-create-db=false"]
    if mode == "reset":
        if confirmation != database: raise BenchmarkError("reset requires --confirm-reset to exactly match --database")
        return ["--do-create-db=true"]
    raise BenchmarkError(f"unknown database mode: {mode}")


def database_workspace(args: argparse.Namespace) -> Path:
    root = (args.database_root or DEFAULT_DATABASE_ROOT).expanduser().resolve()
    return root / args.database_id


def validate_database_manifest(path: Path, expected_id: str | None = None) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    required = {"schema_version", "kind", "database_id", "created_at", "database", "binding"}
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "greptimedb-database" or not required.issubset(manifest):
        raise BenchmarkError(f"malformed database manifest: {path / 'manifest.json'}")
    if expected_id is not None and manifest["database_id"] != expected_id:
        raise BenchmarkError(f"database workspace identity mismatch: {path}")
    if not isinstance(manifest["database_id"], str) or not isinstance(manifest["database"], str):
        raise BenchmarkError(f"malformed database manifest: {path / 'manifest.json'}")
    binding = manifest["binding"]
    binding_fields = {"dataset_id", "spec", "format", "bytes", "sha256"}
    if binding is not None and (not isinstance(binding, dict) or set(binding) != binding_fields or not isinstance(binding.get("spec"), dict)):
        raise BenchmarkError(f"malformed database binding: {path / 'manifest.json'}")
    return manifest


def prepare_database_workspace(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    path = database_workspace(args); manifest_path = path / "manifest.json"
    if manifest_path.exists():
        manifest = validate_database_manifest(path, args.database_id)
        if manifest["database"] != args.database:
            raise BenchmarkError("managed workspace is bound to a different SQL database")
    else:
        (path / "data").mkdir(parents=True, exist_ok=True); (path / "logs").mkdir(parents=True, exist_ok=True)
        manifest = {"schema_version": SCHEMA_VERSION, "kind": "greptimedb-database", "database_id": args.database_id, "created_at": utc_now(), "updated_at": utc_now(), "database": args.database, "binding": None}
        save_json(manifest_path, manifest)
    return path, manifest


@contextlib.contextmanager
def lock_database(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise BenchmarkError(f"managed database workspace is locked: {path}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN); os.close(descriptor)


def dataset_binding(dataset: dict[str, Any]) -> dict[str, Any]:
    return {"dataset_id": dataset["dataset_id"], "spec": dataset["spec"], "format": dataset["format"], "bytes": dataset["bytes"], "sha256": dataset["sha256"]}


def next_attempt(events: Sequence[dict[str, Any]], query_type: str | None = None) -> int:
    matching = [event for event in events if query_type is None or event.get("query_type") == query_type]
    return max((int(event["attempt"]) for event in matching), default=0) + 1


def load_data(args: argparse.Namespace, run_dir: Path, manifest: dict[str, Any], endpoint: str, managed: bool, database_manifest: dict[str, Any] | None = None, database_path: Path | None = None) -> None:
    input_path = generate_data(args, run_dir, manifest); dataset = manifest["dataset"]
    mode = args.database_mode or ("create" if managed else None)
    if mode is None: raise BenchmarkError("external loads require --database-mode=create|reuse|reset")
    binding = dataset_binding(dataset)
    if managed and database_manifest is not None:
        current = database_manifest["binding"]
        if current == binding and mode != "reset":
            manifest["events"]["loads"].append({"attempt": next_attempt(manifest["events"]["loads"]), "database": args.database, "database_mode": "reuse", "status": "reused", "dataset_id": dataset["dataset_id"], "started_at": utc_now(), "finished_at": utc_now()}); save_manifest(run_dir, manifest); return
        if current is not None and current != binding and mode != "reset":
            raise BenchmarkError("managed database contains a different dataset; use a confirmed reset to rebind it")
    ensure_binaries(run_dir, ["load"], args.rebuild)
    attempt = next_attempt(manifest["events"]["loads"]); log_path = run_dir / "logs" / f"load-run-{attempt:03d}.log"; result_path = run_dir / "results" / f"load-run-{attempt:03d}.json"
    event = {"attempt": attempt, "database": args.database, "database_mode": mode, "dataset_id": dataset["dataset_id"], "log": relative(run_dir, log_path), "status": "running", "started_at": utc_now()}
    manifest["events"]["loads"].append(event); save_manifest(run_dir, manifest)
    workload = manifest["workload"]
    command = [str(REPO_ROOT / "bin" / BINARIES["load"]), f"--urls={endpoint}", f"--file={input_path}", f"--db-name={args.database}", f"--batch-size={workload['batch_size']}", "--gzip=false", f"--workers={workload['load_workers']}", "--reporting-period=10s", f"--results-file={result_path}", *database_mode_args(mode, args.database, args.confirm_reset)]
    try: run_tee(command, log_path)
    except Exception:
        event.update(status="failed", finished_at=utc_now()); save_manifest(run_dir, manifest); raise
    event.update(status="completed", finished_at=utc_now(), results=relative(run_dir, result_path)); save_manifest(run_dir, manifest)
    if managed and database_manifest is not None and database_path is not None:
        database_manifest["binding"] = binding; database_manifest["updated_at"] = utc_now(); save_json(database_path / "manifest.json", database_manifest)


def run_queries(args: argparse.Namespace, run_dir: Path, manifest: dict[str, Any], endpoint: str, database_manifest: dict[str, Any] | None = None) -> None:
    query_dir = generate_queries(args, run_dir, manifest); set_manifest = validate_query_set(query_dir, manifest["query_set"]["spec"])
    if database_manifest is not None:
        binding = database_manifest.get("binding")
        if binding is None or binding["dataset_id"] != manifest["dataset"]["dataset_id"] or binding["spec"] != manifest["dataset"]["spec"]:
            raise BenchmarkError("managed database is not loaded with the query set's dataset")
    ensure_binaries(run_dir, ["query"], args.rebuild); workload = manifest["workload"]
    for query_type in set_manifest["spec"]["query_counts"]:
        attempt = next_attempt(manifest["events"]["queries"], query_type); log_path = run_dir / "logs" / f"query-{query_type}-run-{attempt:03d}.log"; result_path = run_dir / "results" / f"query-{query_type}-run-{attempt:03d}.json"
        metadata = set_manifest["files"][query_type]
        event = {"query_type": query_type, "attempt": attempt, "database": args.database, "query_set_id": set_manifest["query_set_id"], "file": metadata["path"], "file_bytes": metadata["bytes"], "file_sha256": metadata["sha256"], "log": relative(run_dir, log_path), "status": "running", "started_at": utc_now()}
        manifest["events"]["queries"].append(event); save_manifest(run_dir, manifest)
        command = [str(REPO_ROOT / "bin" / BINARIES["query"]), f"--file={query_file_path(query_dir, query_type)}", f"--db-name={args.database}", f"--urls={endpoint}", f"--workers={workload['query_workers']}", "--print-interval=0", f"--results-file={result_path}"]
        try: run_tee(command, log_path)
        except Exception:
            event.update(status="failed", finished_at=utc_now()); save_manifest(run_dir, manifest); raise
        event.update(status="completed", finished_at=utc_now(), results=relative(run_dir, result_path)); save_manifest(run_dir, manifest)


def endpoint_ready(endpoint: str) -> bool:
    data = urllib.parse.urlencode({"sql": "SHOW DATABASES"}).encode(); request = urllib.request.Request(endpoint.rstrip("/") + "/v1/sql", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=2) as response: return response.status == 200
    except (OSError, urllib.error.URLError): return False


def check_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try: sock.bind(("127.0.0.1", port))
        except OSError as exc: raise BenchmarkError(f"managed GreptimeDB HTTP port {port} is unavailable") from exc


@contextlib.contextmanager
def connection(args: argparse.Namespace, run_dir: Path) -> Iterator[tuple[str, bool, dict[str, Any] | None, Path | None]]:
    if bool(args.greptime_bin) == bool(args.endpoint): raise BenchmarkError("provide exactly one of --greptime-bin or --endpoint")
    if args.endpoint:
        yield args.endpoint.rstrip("/"), False, None, None; return
    workspace, database_manifest = prepare_database_workspace(args)
    with lock_database(workspace):
        binary = args.greptime_bin.resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK): raise BenchmarkError(f"GreptimeDB binary is not executable: {binary}")
        check_port_available(args.http_port); endpoint = f"http://127.0.0.1:{args.http_port}"; process_log_path = run_dir / "logs" / "greptimedb-process.log"; process_log = process_log_path.open("a", encoding="utf-8")
        command = [str(binary), "standalone", "start", "--http-addr", f"127.0.0.1:{args.http_port}", "--influxdb-enable", "--data-home", str(workspace / "data"), "--log-dir", str(workspace / "logs")]
        process_log.write(f"$ {display_command(command)}\n"); process_log.flush(); process = subprocess.Popen(command, cwd=workspace, stdout=process_log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + args.startup_timeout
            while time.monotonic() < deadline:
                if process.poll() is not None: raise BenchmarkError(f"GreptimeDB exited during startup; see {process_log_path}")
                if endpoint_ready(endpoint): break
                time.sleep(0.5)
            else: raise BenchmarkError(f"GreptimeDB was not ready within {args.startup_timeout}s; see {process_log_path}")
            yield endpoint, True, database_manifest, workspace
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            process_log.close()


def add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path); parser.add_argument("--run-root", type=Path)
    parser.add_argument("--profile", choices=sorted(PROFILES)); parser.add_argument("--start"); parser.add_argument("--end"); parser.add_argument("--scale", type=int); parser.add_argument("--seed", type=int); parser.add_argument("--log-interval")
    parser.add_argument("--load-workers", type=int); parser.add_argument("--query-workers", type=int); parser.add_argument("--batch-size", type=int); parser.add_argument("--queries", type=int, help="count for every selected query type")
    parser.add_argument("--query-type", action="append", choices=QUERY_TYPES); parser.add_argument("--query-root", type=Path); parser.add_argument("--regenerate", action="store_true"); parser.add_argument("--rebuild", action="store_true"); parser.add_argument("--dataset-root", type=Path)
    dataset = parser.add_mutually_exclusive_group(); dataset.add_argument("--dataset-id"); dataset.add_argument("--dataset-path", type=Path)


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--greptime-bin", type=Path); parser.add_argument("--endpoint"); parser.add_argument("--http-port", type=int, default=4000); parser.add_argument("--startup-timeout", type=int, default=60); parser.add_argument("--database", help=f"SQL database name (default: {DEFAULT_DATABASE})"); parser.add_argument("--database-id"); parser.add_argument("--database-root", type=Path)


def add_load_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-mode", choices=("create", "reuse", "reset")); parser.add_argument("--confirm-reset")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__); subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build"); build.add_argument("--run-dir", type=Path); build.add_argument("--run-root", type=Path); build.add_argument("--rebuild", action="store_true")
    generate = subparsers.add_parser("generate"); add_run_options(generate); generate.add_argument("--only", choices=("all", "data", "queries"), default="all")
    load = subparsers.add_parser("load"); add_run_options(load); add_connection_options(load); add_load_options(load)
    query = subparsers.add_parser("query"); add_run_options(query); add_connection_options(query)
    all_command = subparsers.add_parser("all"); add_run_options(all_command); add_connection_options(all_command); add_load_options(all_command)
    summarize = subparsers.add_parser("summarize"); summarize.add_argument("--run-dir", required=True, type=Path)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    for name in ("scale", "load_workers", "query_workers", "batch_size", "queries"):
        value = getattr(args, name, None)
        if value is not None and value <= 0: raise BenchmarkError(f"--{name.replace('_', '-')} must be positive")
    for name in ("dataset_id", "database_id"):
        value = getattr(args, name, None)
        if value and not ID_RE.fullmatch(value): raise BenchmarkError(f"--{name.replace('_', '-')} contains invalid characters")
    if args.command in ("load", "query", "all"):
        if bool(args.greptime_bin) == bool(args.endpoint): raise BenchmarkError("provide exactly one of --greptime-bin or --endpoint")
        if args.greptime_bin and not args.database_id: raise BenchmarkError("managed GreptimeDB requires --database-id")
        if args.endpoint and args.database_id: raise BenchmarkError("--database-id is only valid with managed GreptimeDB")
        if args.endpoint:
            parsed = urllib.parse.urlparse(args.endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc: raise BenchmarkError("--endpoint must be an absolute HTTP or HTTPS URL")
    if args.command in ("load", "all"):
        if args.endpoint and args.database_mode is None: raise BenchmarkError("external loads require --database-mode=create|reuse|reset")
        if args.database_mode == "reset" and args.confirm_reset != args.database: raise BenchmarkError("reset requires --confirm-reset to exactly match --database")


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv); run_dir: Path | None = None; manifest: dict[str, Any] | None = None
    try:
        resolve_database(args); validate_args(args)
        if args.command == "summarize":
            run_dir = args.run_dir.resolve(); manifest = read_json(run_dir / "manifest.json"); validate_run_manifest(manifest, run_dir / "manifest.json"); summary = write_summary(run_dir, manifest); print(run_dir / "summary.md"); return 1 if summary["failures"] else 0
        if args.command == "build":
            run_dir = args.run_dir.resolve() if args.run_dir else new_run_dir((args.run_root or DEFAULT_RUN_ROOT).resolve()); (run_dir / "logs").mkdir(parents=True, exist_ok=True); (run_dir / "results").mkdir(parents=True, exist_ok=True); ensure_binaries(run_dir, list(BINARIES), args.rebuild); print(run_dir); return 0
        run_dir, manifest = prepare_run(args)
        if args.command == "generate":
            if args.only in ("all", "data"): generate_data(args, run_dir, manifest)
            if args.only in ("all", "queries"): generate_queries(args, run_dir, manifest)
        elif args.command in ("load", "query", "all"):
            with connection(args, run_dir) as (endpoint, managed, database_manifest, database_path):
                manifest["target"] = {"mode": "managed" if managed else "external", "endpoint": endpoint, "database": args.database, "database_id": args.database_id if managed else None}; save_manifest(run_dir, manifest)
                if args.command in ("load", "all"): load_data(args, run_dir, manifest, endpoint, managed, database_manifest, database_path)
                if args.command in ("query", "all"): run_queries(args, run_dir, manifest, endpoint, database_manifest)
        summary = write_summary(run_dir, manifest); print(f"Run directory: {run_dir}"); print(f"Summary: {run_dir / 'summary.md'}"); return 1 if summary["failures"] else 0
    except (BenchmarkError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        if run_dir is not None and manifest is not None:
            try: write_summary(run_dir, manifest)
            except OSError: pass
        print(f"error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
