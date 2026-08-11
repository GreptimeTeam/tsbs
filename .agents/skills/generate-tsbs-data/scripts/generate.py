#!/usr/bin/env python3
"""Generate and manage reusable TSBS benchmark datasets."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_DATASET_ROOT = REPO_ROOT / ".benchmarks" / "datasets"
GENERATOR = REPO_ROOT / "bin" / "tsbs_generate_data"
SCHEMA_VERSION = 1
FORMAT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

PROFILES = {
    "manual": {
        "use_case": "cpu-only",
        "start": "2023-06-11T00:00:00Z",
        "end": "2023-06-14T00:00:00Z",
        "scale": 4000,
        "seed": 123,
        "log_interval": "10s",
    },
    "smoke": {
        "use_case": "cpu-only",
        "start": "2023-06-11T00:00:00Z",
        "end": "2023-06-12T00:00:00Z",
        "scale": 10,
        "seed": 123,
        "log_interval": "10s",
    },
}


class DatasetError(RuntimeError):
    """Raised for an actionable dataset error."""


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
        raise DatasetError(f"missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"invalid JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetError(f"manifest must be an object: {path}")
    return value


def logical_spec(
    args: argparse.Namespace,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if args.profile:
        spec = dict(PROFILES[args.profile])
    elif base is not None:
        spec = dict(base)
    else:
        spec = dict(PROFILES["manual"])
    for name in ("use_case", "start", "end", "scale", "seed", "log_interval"):
        value = getattr(args, name, None)
        if value is not None:
            spec[name] = value
    return spec


def automatic_dataset_id(spec: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json({"schema_version": SCHEMA_VERSION, "spec": spec}).encode()).hexdigest()
    use_case = re.sub(r"[^a-z0-9]+", "-", str(spec["use_case"]).lower()).strip("-") or "data"
    return f"{use_case}-s{spec['scale']}-{digest[:12]}"


def dataset_root(args: argparse.Namespace) -> Path:
    configured = args.dataset_root or os.environ.get("TSBS_DATASET_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DATASET_ROOT


def resolve_dataset_path(args: argparse.Namespace, spec: dict[str, Any] | None = None) -> Path:
    if getattr(args, "dataset_path", None):
        return args.dataset_path.expanduser().resolve()
    dataset_id = getattr(args, "dataset_id", None)
    if dataset_id:
        if not ID_RE.fullmatch(dataset_id):
            raise DatasetError("--dataset-id may contain only letters, digits, '.', '_', and '-'")
    elif spec is not None:
        dataset_id = automatic_dataset_id(spec)
    else:
        raise DatasetError("provide --dataset-id or --dataset-path")
    return dataset_root(args) / dataset_id


def validate_format(format_name: str) -> None:
    if not FORMAT_RE.fullmatch(format_name):
        raise DatasetError("--format must contain only lowercase letters, digits, '_' and '-'")


def validate_dataset_manifest(dataset_dir: Path, expected_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = read_json(dataset_dir / "dataset.json")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DatasetError(f"unsupported dataset schema in {dataset_dir / 'dataset.json'}")
    if not isinstance(manifest.get("spec"), dict):
        raise DatasetError(f"dataset manifest has no logical specification: {dataset_dir}")
    if expected_spec is not None and manifest["spec"] != expected_spec:
        raise DatasetError(f"dataset settings do not match requested workload: {dataset_dir}")
    return manifest


def validate_variant(dataset_dir: Path, format_name: str) -> dict[str, Any]:
    variant_dir = dataset_dir / "formats" / format_name
    manifest = read_json(variant_dir / "manifest.json")
    if manifest.get("status") != "completed":
        raise DatasetError(f"dataset format variant is not complete: {variant_dir}")
    if manifest.get("format") != format_name:
        raise DatasetError(f"dataset format manifest mismatch: {variant_dir}")
    artifact = variant_dir / str(manifest.get("artifact", "data"))
    if not artifact.is_file():
        raise DatasetError(f"missing dataset artifact: {artifact}")
    actual_size = artifact.stat().st_size
    actual_checksum = sha256_file(artifact)
    if actual_size != manifest.get("bytes") or actual_checksum != manifest.get("sha256"):
        raise DatasetError(f"dataset artifact checksum mismatch: {artifact}")
    return manifest


def command_text(command: Sequence[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in command)


def run_build(log_path: Path, rebuild: bool) -> None:
    if GENERATOR.is_file() and not rebuild:
        return
    GENERATOR.parent.mkdir(parents=True, exist_ok=True)
    command = ["go", "build", "-o", str(GENERATOR), "./cmd/tsbs_generate_data"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        header = f"$ {command_text(command)}\n"
        log.write(header)
        print(header, end="", file=sys.stderr)
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
            log.write(line)
            sys.stderr.write(line)
        process.stdout.close()
        if process.wait():
            raise DatasetError(f"failed to build tsbs_generate_data; see {log_path}")


def git_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def generate_variant(
    dataset_dir: Path,
    dataset_manifest: dict[str, Any],
    format_name: str,
    *,
    regenerate: bool,
    rebuild: bool,
) -> dict[str, Any]:
    validate_format(format_name)
    variant_dir = dataset_dir / "formats" / format_name
    log_path = variant_dir / "generate.log"
    artifact = variant_dir / "data"
    manifest_path = variant_dir / "manifest.json"
    if manifest_path.exists() and not regenerate:
        existing = read_json(manifest_path)
        if existing.get("status") == "completed":
            if rebuild:
                run_build(log_path, True)
            manifest = validate_variant(dataset_dir, format_name)
            return result(dataset_dir, dataset_manifest, manifest, reused=True)

    variant_dir.mkdir(parents=True, exist_ok=True)
    run_build(log_path, rebuild)
    spec = dataset_manifest["spec"]
    command = [
        str(GENERATOR),
        f"--use-case={spec['use_case']}",
        f"--seed={spec['seed']}",
        f"--scale={spec['scale']}",
        f"--timestamp-start={spec['start']}",
        f"--timestamp-end={spec['end']}",
        f"--log-interval={spec['log_interval']}",
        f"--format={format_name}",
    ]
    temporary = artifact.with_name(f"data.tmp-{os.getpid()}")
    started_at = utc_now()
    with log_path.open("a", encoding="utf-8") as log:
        header = f"$ {command_text(command)}\n"
        log.write(header)
        print(header, end="", file=sys.stderr)
        with temporary.open("wb") as output:
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
                log.write(line)
                sys.stderr.write(line)
            process.stderr.close()
            return_code = process.wait()
    if return_code:
        temporary.unlink(missing_ok=True)
        if not artifact.exists():
            save_json(
                manifest_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "format": format_name,
                    "status": "failed",
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "log": "generate.log",
                },
            )
        raise DatasetError(f"tsbs_generate_data failed with exit code {return_code}; see {log_path}")

    checksum = sha256_file(temporary)
    size = temporary.stat().st_size
    os.replace(temporary, artifact)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "format": format_name,
        "status": "completed",
        "artifact": "data",
        "bytes": size,
        "sha256": checksum,
        "started_at": started_at,
        "finished_at": utc_now(),
        "log": "generate.log",
        "generator": {
            "binary": "bin/tsbs_generate_data",
            "binary_sha256": sha256_file(GENERATOR),
            "git_revision": git_revision(),
        },
    }
    save_json(manifest_path, manifest)
    return result(dataset_dir, dataset_manifest, manifest, reused=False)


def result(
    dataset_dir: Path,
    dataset_manifest: dict[str, Any],
    variant_manifest: dict[str, Any],
    *,
    reused: bool,
) -> dict[str, Any]:
    format_name = str(variant_manifest["format"])
    return {
        "dataset_id": dataset_manifest["dataset_id"],
        "dataset_path": str(dataset_dir),
        "format": format_name,
        "data_path": str(dataset_dir / "formats" / format_name / str(variant_manifest["artifact"])),
        "bytes": variant_manifest["bytes"],
        "sha256": variant_manifest["sha256"],
        "spec": dataset_manifest["spec"],
        "reused": reused,
    }


def logical_result(dataset_dir: Path, dataset_manifest: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    """Return metadata for a logical dataset without requiring a format artifact."""
    return {
        "dataset_id": dataset_manifest["dataset_id"],
        "dataset_path": str(dataset_dir),
        "spec": dataset_manifest["spec"],
        "reused": reused,
    }


def prepare_dataset(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    explicit_selection = bool(args.dataset_id or args.dataset_path)
    dataset_dir = resolve_dataset_path(args) if explicit_selection else None
    if dataset_dir is not None and (dataset_dir / "dataset.json").exists():
        manifest = validate_dataset_manifest(dataset_dir)
        requested = logical_spec(args, manifest["spec"])
        if requested != manifest["spec"]:
            raise DatasetError(f"dataset settings do not match requested workload: {dataset_dir}")
        return dataset_dir, manifest

    spec = logical_spec(args)
    dataset_dir = dataset_dir or resolve_dataset_path(args, spec)
    manifest_path = dataset_dir / "dataset.json"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": args.dataset_id or automatic_dataset_id(spec),
        "created_at": utc_now(),
        "spec": spec,
    }
    save_json(manifest_path, manifest)
    return dataset_dir, manifest


def list_datasets(args: argparse.Namespace) -> list[dict[str, Any]]:
    root = dataset_root(args)
    if not root.exists():
        return []
    datasets: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path / "dataset.json").is_file():
            continue
        try:
            manifest = validate_dataset_manifest(path)
            formats = []
            if (path / "formats").is_dir():
                for child in sorted(path.joinpath("formats").iterdir()):
                    if not child.is_dir() or not (child / "manifest.json").is_file():
                        continue
                    variant_manifest = read_json(child / "manifest.json")
                    if variant_manifest.get("status") == "completed":
                        formats.append(child.name)
            datasets.append(
                {
                    "dataset_id": manifest.get("dataset_id", path.name),
                    "dataset_path": str(path.resolve()),
                    "spec": manifest["spec"],
                    "formats": formats,
                }
            )
        except DatasetError:
            continue
    return datasets


def select_existing(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    path = resolve_dataset_path(args)
    return path, validate_dataset_manifest(path)


def verify_dataset(args: argparse.Namespace) -> dict[str, Any]:
    path, manifest = select_existing(args)
    if args.format:
        formats = [args.format]
    else:
        formats_dir = path / "formats"
        formats = (
            sorted(child.name for child in formats_dir.iterdir() if child.is_dir())
            if formats_dir.exists()
            else []
        )
    if not formats:
        raise DatasetError(f"dataset has no format variants: {path}")
    variants = []
    for format_name in formats:
        validate_format(format_name)
        variant = validate_variant(path, format_name)
        variants.append(result(path, manifest, variant, reused=True))
    return {"dataset_id": manifest["dataset_id"], "dataset_path": str(path), "variants": variants}


def print_output(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True))
        return
    if isinstance(value, list):
        for item in value:
            print(f"{item['dataset_id']}\t{','.join(item['formats'])}\t{item['dataset_path']}")
    elif isinstance(value, dict) and "data_path" in value:
        action = "reused" if value.get("reused") else "generated"
        print(f"Dataset {action}: {value['dataset_id']} ({value['format']})")
        print(f"Data: {value['data_path']}")
        print(f"SHA-256: {value['sha256']}")
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def add_root_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset-root", type=Path)


def add_selection_options(parser: argparse.ArgumentParser, required: bool = False) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--dataset-id")
    group.add_argument("--dataset-path", type=Path)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate or reuse a format variant")
    add_root_options(generate)
    add_selection_options(generate)
    generate.add_argument("--profile", choices=sorted(PROFILES))
    generate.add_argument("--format", required=True)
    generate.add_argument("--use-case")
    generate.add_argument("--start")
    generate.add_argument("--end")
    generate.add_argument("--scale", type=int)
    generate.add_argument("--seed", type=int)
    generate.add_argument("--log-interval")
    generate.add_argument("--regenerate", action="store_true")
    generate.add_argument("--rebuild", action="store_true")
    generate.add_argument("--result-file", type=Path)
    generate.add_argument("--json", action="store_true")

    prepare = subparsers.add_parser("prepare", help="create or reuse logical dataset metadata only")
    add_root_options(prepare)
    add_selection_options(prepare)
    prepare.add_argument("--profile", choices=sorted(PROFILES))
    prepare.add_argument("--use-case")
    prepare.add_argument("--start")
    prepare.add_argument("--end")
    prepare.add_argument("--scale", type=int)
    prepare.add_argument("--seed", type=int)
    prepare.add_argument("--log-interval")
    prepare.add_argument("--result-file", type=Path)
    prepare.add_argument("--json", action="store_true")

    list_command = subparsers.add_parser("list", help="list cached logical datasets")
    add_root_options(list_command)
    list_command.add_argument("--json", action="store_true")

    inspect = subparsers.add_parser("inspect", help="show a logical dataset manifest")
    add_root_options(inspect)
    add_selection_options(inspect, required=True)
    inspect.add_argument("--json", action="store_true")

    verify = subparsers.add_parser("verify", help="verify cached format checksums")
    add_root_options(verify)
    add_selection_options(verify, required=True)
    verify.add_argument("--format")
    verify.add_argument("--json", action="store_true")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if getattr(args, "scale", None) is not None and args.scale <= 0:
        raise DatasetError("--scale must be positive")
    if getattr(args, "format", None):
        validate_format(args.format)


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        validate_args(args)
        if args.command == "generate":
            path, manifest = prepare_dataset(args)
            output = generate_variant(
                path,
                manifest,
                args.format,
                regenerate=args.regenerate,
                rebuild=args.rebuild,
            )
        elif args.command == "prepare":
            selected_before = resolve_dataset_path(args, logical_spec(args))
            existed = (selected_before / "dataset.json").is_file()
            path, manifest = prepare_dataset(args)
            output = logical_result(path, manifest, reused=existed)
        elif args.command == "list":
            output = list_datasets(args)
        elif args.command == "inspect":
            path, manifest = select_existing(args)
            output = {**manifest, "dataset_path": str(path)}
        else:
            output = verify_dataset(args)
        if getattr(args, "result_file", None):
            save_json(args.result_file.resolve(), output)
        print_output(output, args.json)
        return 0
    except (DatasetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
