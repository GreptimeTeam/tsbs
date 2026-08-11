#!/usr/bin/env python3
"""Run staged GreptimeDB TSBS benchmarks with durable logs and summaries."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Sequence

from summarize import write_summary


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".benchmarks" / "greptimedb"
DATASET_RUNNER = REPO_ROOT / ".agents" / "skills" / "generate-tsbs-data" / "scripts" / "generate.py"

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

BINARIES = {
    "queries": "tsbs_generate_queries",
    "load": "tsbs_load_greptime",
    "query": "tsbs_run_queries_influx",
}
BUILT_THIS_PROCESS: set[str] = set()


class BenchmarkError(RuntimeError):
    """Raised for an actionable benchmark failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_dir() -> Path:
    DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    base = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = DEFAULT_OUTPUT_ROOT / base
    suffix = 1
    while candidate.exists():
        candidate = DEFAULT_OUTPUT_ROOT / f"{base}-{suffix:02d}"
        suffix += 1
    return candidate


def add_one_second(timestamp: str) -> str:
    parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return (parsed + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    temp = run_dir / "manifest.json.tmp"
    temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, run_dir / "manifest.json")


def prepare_run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    run_dir = args.run_dir.resolve() if args.run_dir else new_run_dir()
    for directory in ("data", "queries", "logs", "results", "greptimedb/data", "greptimedb/logs"):
        (run_dir / directory).mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        profile = args.profile or "manual"
        manifest = {
            "run_id": run_dir.name,
            "created_at": utc_now(),
            "profile": profile,
            "database": getattr(args, "database", "benchmark"),
            "workload": json.loads(json.dumps(PROFILES[profile])),
            "events": {"loads": [], "queries": []},
        }

    workload = manifest["workload"]
    for attr in ("start", "end", "scale", "seed", "log_interval", "load_workers", "query_workers", "batch_size"):
        value = getattr(args, attr, None)
        if value is not None:
            workload[attr] = value
    if getattr(args, "queries", None) is not None:
        selected = args.query_type or QUERY_TYPES
        for query_type in selected:
            workload["query_counts"][query_type] = args.queries
    if hasattr(args, "database"):
        manifest["database"] = args.database
    save_manifest(run_dir, manifest)
    return run_dir, manifest


def relative(run_dir: Path, path: Path) -> str:
    return str(path.relative_to(run_dir))


def display_command(command: Sequence[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in command)


def run_tee(
    command: Sequence[str],
    log_path: Path,
    *,
    stdout_path: Path | None = None,
    append: bool = False,
) -> None:
    mode = "a" if append else "w"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open(mode, encoding="utf-8") as log:
        header = f"$ {display_command(command)}\n"
        log.write(header)
        log.flush()
        print(header, end="")
        if stdout_path is None:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                log.write(line)
            process.stdout.close()
            return_code = process.wait()
        else:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_output = stdout_path.with_name(stdout_path.name + ".tmp")
            with temporary_output.open("wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=REPO_ROOT,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                assert process.stderr is not None
                for line in process.stderr:
                    sys.stdout.write(line)
                    log.write(line)
                process.stderr.close()
                return_code = process.wait()
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
        name = BINARIES[stage]
        target = REPO_ROOT / "bin" / name
        marker = run_dir / "results" / f"built-{name}"
        if not binary_needs_build(run_dir, name, target, rebuild):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        run_tee(
            ["go", "build", "-o", str(target), f"./cmd/{name}"],
            run_dir / "logs" / "build.log",
            append=True,
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(utc_now() + "\n", encoding="utf-8")
        BUILT_THIS_PROCESS.add(name)


def query_path(run_dir: Path, query_type: str) -> Path:
    return run_dir / "queries" / f"greptime-queries-{query_type}.dat"


def query_generation_spec(workload: dict[str, Any], query_type: str) -> dict[str, Any]:
    return {
        "use_case": "devops",
        "seed": workload["seed"],
        "scale": workload["scale"],
        "timestamp_start": workload["start"],
        "timestamp_end": add_one_second(workload["end"]),
        "queries": workload["query_counts"][query_type],
        "query_type": query_type,
        "format": "greptime",
    }


def dataset_selection_args(
    args: argparse.Namespace,
    manifest: dict[str, Any],
) -> list[str]:
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


def validate_dataset_result(
    manifest: dict[str, Any],
    dataset: dict[str, Any],
    regenerate: bool,
) -> None:
    if dataset["spec"]["use_case"] != "cpu-only":
        raise BenchmarkError("GreptimeDB benchmark requires a cpu-only dataset")
    pinned = manifest.get("dataset")
    if pinned and pinned.get("sha256") != dataset.get("sha256") and not regenerate:
        raise BenchmarkError(
            "the pinned dataset checksum changed; use --regenerate only if replacement was intentional"
        )


def generate_data(
    args: argparse.Namespace,
    run_dir: Path,
    manifest: dict[str, Any],
) -> Path:
    legacy = run_dir / "data" / "influx-data.lp"
    if legacy.exists() and not manifest.get("dataset") and not (
        args.dataset_root or args.dataset_id or args.dataset_path or args.regenerate
    ):
        print(f"Reusing legacy generated data: {legacy}")
        manifest["generated_data"] = relative(run_dir, legacy)
        save_manifest(run_dir, manifest)
        return legacy

    workload = manifest["workload"]
    selecting_unpinned = not manifest.get("dataset") and bool(args.dataset_id or args.dataset_path)
    result_path = run_dir / "results" / "dataset.json"
    command = [
        sys.executable,
        str(DATASET_RUNNER),
        "generate",
        "--format",
        "influx",
        "--use-case",
        "cpu-only",
        "--result-file",
        str(result_path),
        *dataset_selection_args(args, manifest),
    ]
    if selecting_unpinned:
        if args.profile:
            command.extend(["--profile", args.profile])
        for option, value in (
            ("--seed", args.seed),
            ("--scale", args.scale),
            ("--start", args.start),
            ("--end", args.end),
            ("--log-interval", args.log_interval),
        ):
            if value is not None:
                command.extend([option, str(value)])
    else:
        command.extend(
            [
                "--seed",
                str(workload["seed"]),
                "--scale",
                str(workload["scale"]),
                "--start",
                workload["start"],
                "--end",
                workload["end"],
                "--log-interval",
                workload["log_interval"],
            ]
        )
    if args.regenerate:
        command.append("--regenerate")
    if args.rebuild:
        command.append("--rebuild")
    run_tee(command, run_dir / "logs" / "generate-data.log")
    dataset = json.loads(result_path.read_text(encoding="utf-8"))
    validate_dataset_result(manifest, dataset, args.regenerate)
    for name in ("start", "end", "scale", "seed", "log_interval"):
        workload[name] = dataset["spec"][name]
    manifest["dataset"] = dataset
    manifest["generated_data"] = dataset["data_path"]
    save_manifest(run_dir, manifest)
    return Path(dataset["data_path"])


def generate_queries(
    run_dir: Path,
    manifest: dict[str, Any],
    query_types: Sequence[str],
    regenerate: bool,
    rebuild: bool,
) -> None:
    ensure_binaries(run_dir, ["queries"], rebuild)
    workload = manifest["workload"]
    generation_specs = manifest.setdefault("query_generation_specs", {})
    for query_type in query_types:
        output = query_path(run_dir, query_type)
        spec = query_generation_spec(workload, query_type)
        if output.exists() and generation_specs.get(query_type) == spec and not regenerate:
            print(f"Reusing generated queries: {output}")
            continue
        command = [
            str(REPO_ROOT / "bin" / BINARIES["queries"]),
            f"--use-case={spec['use_case']}",
            f"--seed={spec['seed']}",
            f"--scale={spec['scale']}",
            f"--timestamp-start={spec['timestamp_start']}",
            f"--timestamp-end={spec['timestamp_end']}",
            f"--queries={spec['queries']}",
            f"--query-type={spec['query_type']}",
            f"--format={spec['format']}",
        ]
        run_tee(command, run_dir / "logs" / f"generate-query-{query_type}.log", stdout_path=output)
        generation_specs[query_type] = spec
        save_manifest(run_dir, manifest)
    manifest["generated_query_types"] = sorted(
        query_type for query_type in QUERY_TYPES if query_path(run_dir, query_type).exists()
    )
    save_manifest(run_dir, manifest)


def database_mode_args(mode: str, database: str, confirmation: str | None) -> list[str]:
    if mode == "create":
        return ["--do-create-db=true", "--do-abort-on-exist=true"]
    if mode == "reuse":
        return ["--do-create-db=false"]
    if mode == "reset":
        if confirmation != database:
            raise BenchmarkError("reset requires --confirm-reset to exactly match --database")
        return ["--do-create-db=true"]
    raise BenchmarkError(f"unknown database mode: {mode}")


def next_attempt(events: Sequence[dict[str, Any]], query_type: str | None = None) -> int:
    matching = [event for event in events if query_type is None or event.get("query_type") == query_type]
    return max((int(event["attempt"]) for event in matching), default=0) + 1


def load_data(
    args: argparse.Namespace,
    run_dir: Path,
    manifest: dict[str, Any],
    endpoint: str,
    managed: bool,
) -> None:
    input_path = generate_data(args, run_dir, manifest)
    ensure_binaries(run_dir, ["load"], args.rebuild)
    mode = args.database_mode or ("create" if managed else None)
    if mode is None:
        raise BenchmarkError("external loads require --database-mode=create|reuse|reset")
    attempt = next_attempt(manifest["events"]["loads"])
    log_path = run_dir / "logs" / f"load-run-{attempt:03d}.log"
    result_path = run_dir / "results" / f"load-run-{attempt:03d}.json"
    event = {
        "attempt": attempt,
        "database": args.database,
        "database_mode": mode,
        "log": relative(run_dir, log_path),
        "status": "running",
        "started_at": utc_now(),
    }
    manifest["events"]["loads"].append(event)
    save_manifest(run_dir, manifest)
    workload = manifest["workload"]
    command = [
        str(REPO_ROOT / "bin" / BINARIES["load"]),
        f"--urls={endpoint}",
        f"--file={input_path}",
        f"--db-name={args.database}",
        f"--batch-size={workload['batch_size']}",
        "--gzip=false",
        f"--workers={workload['load_workers']}",
        "--reporting-period=10s",
        f"--results-file={result_path}",
        *database_mode_args(mode, args.database, args.confirm_reset),
    ]
    try:
        run_tee(command, log_path)
    except Exception:
        event["status"] = "failed"
        event["finished_at"] = utc_now()
        save_manifest(run_dir, manifest)
        raise
    event["status"] = "completed"
    event["finished_at"] = utc_now()
    event["results"] = relative(run_dir, result_path)
    save_manifest(run_dir, manifest)


def run_queries(
    args: argparse.Namespace,
    run_dir: Path,
    manifest: dict[str, Any],
    endpoint: str,
) -> None:
    query_types = args.query_type or list(QUERY_TYPES)
    generate_queries(run_dir, manifest, query_types, args.regenerate, args.rebuild)
    ensure_binaries(run_dir, ["query"], args.rebuild)
    workload = manifest["workload"]
    for query_type in query_types:
        for _ in range(args.repeat):
            attempt = next_attempt(manifest["events"]["queries"], query_type)
            log_path = run_dir / "logs" / f"query-{query_type}-run-{attempt:03d}.log"
            result_path = run_dir / "results" / f"query-{query_type}-run-{attempt:03d}.json"
            event = {
                "query_type": query_type,
                "attempt": attempt,
                "database": args.database,
                "log": relative(run_dir, log_path),
                "status": "running",
                "started_at": utc_now(),
            }
            manifest["events"]["queries"].append(event)
            save_manifest(run_dir, manifest)
            command = [
                str(REPO_ROOT / "bin" / BINARIES["query"]),
                f"--file={query_path(run_dir, query_type)}",
                f"--db-name={args.database}",
                f"--urls={endpoint}",
                f"--workers={workload['query_workers']}",
                "--print-interval=0",
                f"--results-file={result_path}",
            ]
            try:
                run_tee(command, log_path)
            except Exception:
                event["status"] = "failed"
                event["finished_at"] = utc_now()
                save_manifest(run_dir, manifest)
                raise
            event["status"] = "completed"
            event["finished_at"] = utc_now()
            event["results"] = relative(run_dir, result_path)
            save_manifest(run_dir, manifest)


def endpoint_ready(endpoint: str) -> bool:
    data = urllib.parse.urlencode({"sql": "SHOW DATABASES"}).encode()
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/sql",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def check_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise BenchmarkError(f"managed GreptimeDB HTTP port {port} is unavailable") from exc


@contextlib.contextmanager
def connection(args: argparse.Namespace, run_dir: Path) -> Iterator[tuple[str, bool]]:
    if bool(args.greptime_bin) == bool(args.endpoint):
        raise BenchmarkError("provide exactly one of --greptime-bin or --endpoint")
    if args.endpoint:
        yield args.endpoint.rstrip("/"), False
        return

    binary = args.greptime_bin.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise BenchmarkError(f"GreptimeDB binary is not executable: {binary}")
    check_port_available(args.http_port)
    endpoint = f"http://127.0.0.1:{args.http_port}"
    process_log_path = run_dir / "logs" / "greptimedb-process.log"
    process_log = process_log_path.open("a", encoding="utf-8")
    command = [
        str(binary),
        "standalone",
        "start",
        "--http-addr",
        f"127.0.0.1:{args.http_port}",
        "--influxdb-enable",
        "--data-home",
        str(run_dir / "greptimedb" / "data"),
        "--log-dir",
        str(run_dir / "greptimedb" / "logs"),
    ]
    process_log.write(f"$ {display_command(command)}\n")
    process_log.flush()
    process = subprocess.Popen(
        command,
        cwd=run_dir,
        stdout=process_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BenchmarkError(f"GreptimeDB exited during startup; see {process_log_path}")
            if endpoint_ready(endpoint):
                break
            time.sleep(0.5)
        else:
            raise BenchmarkError(f"GreptimeDB was not ready within {args.startup_timeout}s; see {process_log_path}")
        yield endpoint, True
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        process_log.close()


def add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--profile", choices=sorted(PROFILES))
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--scale", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--log-interval")
    parser.add_argument("--load-workers", type=int)
    parser.add_argument("--query-workers", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--queries", type=int, help="override the generated count for selected query types")
    parser.add_argument("--query-type", action="append", choices=QUERY_TYPES)
    parser.add_argument("--regenerate", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dataset-root", type=Path)
    dataset = parser.add_mutually_exclusive_group()
    dataset.add_argument("--dataset-id")
    dataset.add_argument("--dataset-path", type=Path)


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--greptime-bin", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--http-port", type=int, default=4000)
    parser.add_argument("--startup-timeout", type=int, default=60)
    parser.add_argument("--database", default="benchmark")


def add_load_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-mode", choices=("create", "reuse", "reset"))
    parser.add_argument("--confirm-reset")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build the three GreptimeDB-specific TSBS tools")
    build.add_argument("--run-dir", type=Path)
    build.add_argument("--rebuild", action="store_true")

    generate = subparsers.add_parser("generate", help="generate data and/or queries")
    add_run_options(generate)
    generate.add_argument("--only", choices=("all", "data", "queries"), default="all")

    load = subparsers.add_parser("load", help="load generated data")
    add_run_options(load)
    add_connection_options(load)
    add_load_options(load)

    query = subparsers.add_parser("query", help="run selected generated queries")
    add_run_options(query)
    add_connection_options(query)
    query.add_argument("--repeat", type=int, default=1)

    all_command = subparsers.add_parser("all", help="generate, load, query, and summarize")
    add_run_options(all_command)
    add_connection_options(all_command)
    add_load_options(all_command)
    all_command.add_argument("--repeat", type=int, default=1)

    summarize = subparsers.add_parser("summarize", help="rebuild summaries from manifest logs")
    summarize.add_argument("--run-dir", required=True, type=Path)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    for name in ("scale", "load_workers", "query_workers", "batch_size", "queries"):
        value = getattr(args, name, None)
        if value is not None and value <= 0:
            raise BenchmarkError(f"--{name.replace('_', '-')} must be positive")
    if getattr(args, "repeat", 1) <= 0:
        raise BenchmarkError("--repeat must be positive")
    if args.command in ("load", "query", "all"):
        if bool(args.greptime_bin) == bool(args.endpoint):
            raise BenchmarkError("provide exactly one of --greptime-bin or --endpoint")
        if args.endpoint:
            parsed = urllib.parse.urlparse(args.endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise BenchmarkError("--endpoint must be an absolute HTTP or HTTPS URL")
    if args.command in ("load", "all"):
        if args.endpoint and args.database_mode is None:
            raise BenchmarkError("external loads require --database-mode=create|reuse|reset")
        if args.database_mode == "reset" and args.confirm_reset != args.database:
            raise BenchmarkError("reset requires --confirm-reset to exactly match --database")


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    run_dir: Path | None = None
    manifest: dict[str, Any] | None = None
    try:
        validate_args(args)
        if args.command == "summarize":
            run_dir = args.run_dir.resolve()
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            summary = write_summary(run_dir, manifest)
            print(run_dir / "summary.md")
            return 1 if summary["failures"] else 0

        if args.command == "build":
            run_dir = args.run_dir.resolve() if args.run_dir else new_run_dir()
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            ensure_binaries(run_dir, list(BINARIES), args.rebuild)
            print(run_dir)
            return 0

        run_dir, manifest = prepare_run(args)
        query_types = args.query_type or list(QUERY_TYPES)
        if args.command == "generate":
            if args.only in ("all", "data"):
                generate_data(args, run_dir, manifest)
            if args.only in ("all", "queries"):
                generate_queries(run_dir, manifest, query_types, args.regenerate, args.rebuild)
        elif args.command in ("load", "query", "all"):
            with connection(args, run_dir) as (endpoint, managed):
                manifest["database"] = args.database
                manifest["connection"] = {"mode": "managed" if managed else "external", "endpoint": endpoint}
                save_manifest(run_dir, manifest)
                if args.command in ("load", "all"):
                    load_data(args, run_dir, manifest, endpoint, managed)
                if args.command in ("query", "all"):
                    run_queries(args, run_dir, manifest, endpoint)
        summary = write_summary(run_dir, manifest)
        print(f"Run directory: {run_dir}")
        print(f"Summary: {run_dir / 'summary.md'}")
        return 1 if summary["failures"] else 0
    except (BenchmarkError, OSError, json.JSONDecodeError) as exc:
        if run_dir is not None and manifest is not None:
            try:
                write_summary(run_dir, manifest)
            except OSError:
                pass
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
