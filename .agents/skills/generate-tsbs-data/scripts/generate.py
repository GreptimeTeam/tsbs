#!/usr/bin/env python3
"""Generate and manage reusable TSBS benchmark datasets."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
sys.path.insert(0, str(REPO_ROOT / ".agents" / "lib"))

from tsbs_environment import TsbsEnvironmentError, resolve_go  # noqa: E402

DEFAULT_DATASET_ROOT = REPO_ROOT / ".benchmarks" / "datasets"
GENERATOR = REPO_ROOT / "bin" / "tsbs_generate_data"
GENERATOR_BUILD_METADATA = REPO_ROOT / "bin" / "tsbs_generate_data.build.json"
SCHEMA_VERSION = 1
FORMAT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GO_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ns|us|µs|ms|s|m|h)")
COMPRESSIONS = ("none", "gzip")

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


def sha256_command() -> list[str]:
    sha256sum = shutil.which("sha256sum")
    if sha256sum:
        return [sha256sum]
    shasum = shutil.which("shasum")
    if shasum:
        return [shasum, "-a", "256"]
    raise DatasetError("SHA-256 verification requires sha256sum or shasum")


def stored_file_sha256(path: Path) -> str:
    """Compute an artifact checksum outside Python's data path."""
    command = [*sha256_command(), str(path)]
    process = subprocess.run(command, check=False, capture_output=True, text=True)
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise DatasetError(f"failed to checksum dataset artifact {path}{suffix}")
    checksum = process.stdout.split(maxsplit=1)[0].lower() if process.stdout.split() else ""
    if re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        raise DatasetError(f"invalid SHA-256 output for dataset artifact {path}")
    return checksum


def gzip_command() -> list[str]:
    gzip_binary = shutil.which("gzip")
    if not gzip_binary:
        raise DatasetError("gzip compression requires the gzip command")
    return [gzip_binary, "-n", "-6", "-c"]


def parse_go_duration(value: str) -> int:
    """Parse a positive Go duration and return nanoseconds."""
    position = 0
    total = Decimal(0)
    units = {
        "ns": Decimal(1),
        "us": Decimal(1_000),
        "µs": Decimal(1_000),
        "ms": Decimal(1_000_000),
        "s": Decimal(1_000_000_000),
        "m": Decimal(60_000_000_000),
        "h": Decimal(3_600_000_000_000),
    }
    try:
        for match in GO_DURATION_RE.finditer(value):
            if match.start() != position:
                raise DatasetError(f"invalid Go duration: {value}")
            total += Decimal(match.group(1)) * units[match.group(2)]
            position = match.end()
    except InvalidOperation as exc:
        raise DatasetError(f"invalid Go duration: {value}") from exc
    if position != len(value) or total <= 0 or total != total.to_integral_value():
        raise DatasetError(f"invalid positive Go duration: {value}")
    return int(total)


def estimated_points(spec: dict[str, Any]) -> int | None:
    if spec.get("use_case") != "cpu-only":
        return None
    try:
        start = dt.datetime.fromisoformat(str(spec["start"]).replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(str(spec["end"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise DatasetError("dataset timestamps must be valid ISO-8601 values") from exc
    elapsed = end - start
    duration_ns = ((elapsed.days * 86_400 + elapsed.seconds) * 1_000_000_000) + elapsed.microseconds * 1_000
    if duration_ns <= 0:
        raise DatasetError("dataset end timestamp must be after start timestamp")
    return int(spec["scale"]) * (duration_ns // parse_go_duration(str(spec["log_interval"])))


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


def automatic_dataset_id(spec: dict[str, Any], compression: str = "none") -> str:
    identity = {"schema_version": SCHEMA_VERSION, "spec": spec}
    if compression != "none":
        identity["compression"] = compression
    digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    use_case = re.sub(r"[^a-z0-9]+", "-", str(spec["use_case"]).lower()).strip("-") or "data"
    return f"{use_case}-s{spec['scale']}-{digest[:12]}"


def dataset_root(args: argparse.Namespace) -> Path:
    configured = args.dataset_root or os.environ.get("TSBS_DATASET_ROOT")
    return Path(configured).expanduser().resolve() if configured else DEFAULT_DATASET_ROOT


def resolve_dataset_path(
    args: argparse.Namespace,
    spec: dict[str, Any] | None = None,
    compression: str = "none",
) -> Path:
    if getattr(args, "dataset_path", None):
        return args.dataset_path.expanduser().resolve()
    dataset_id = getattr(args, "dataset_id", None)
    if dataset_id:
        if not ID_RE.fullmatch(dataset_id):
            raise DatasetError("--dataset-id may contain only letters, digits, '.', '_', and '-'")
    elif spec is not None:
        dataset_id = automatic_dataset_id(spec, compression)
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
    if manifest.get("compression", "none") not in COMPRESSIONS:
        raise DatasetError(f"unsupported dataset compression in {dataset_dir / 'dataset.json'}")
    if expected_spec is not None and manifest["spec"] != expected_spec:
        raise DatasetError(f"dataset settings do not match requested workload: {dataset_dir}")
    return manifest


def manifest_compression(manifest: dict[str, Any]) -> str:
    return str(manifest.get("compression", "none"))


def variant_dir(dataset_dir: Path, format_name: str) -> Path:
    return dataset_dir / "formats" / format_name


def validate_variant(
    dataset_dir: Path,
    format_name: str,
    *,
    verify_checksum: bool = True,
) -> tuple[Path, dict[str, Any]]:
    path = variant_dir(dataset_dir, format_name)
    manifest = read_json(path / "manifest.json")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DatasetError(
            f"unsupported dataset format schema: {path / 'manifest.json'}; "
            "regenerate it with --regenerate"
        )
    if manifest.get("status") != "completed":
        raise DatasetError(f"dataset format variant is not complete: {path}")
    if manifest.get("format") != format_name:
        raise DatasetError(f"dataset format manifest mismatch: {path}")
    artifact = path / str(manifest.get("artifact", "data"))
    if not artifact.is_file():
        raise DatasetError(f"missing dataset artifact: {artifact}")
    recorded_size = manifest.get("bytes")
    recorded_checksum = manifest.get("sha256")
    if (
        isinstance(recorded_size, bool)
        or not isinstance(recorded_size, int)
        or recorded_size < 0
        or not isinstance(recorded_checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", recorded_checksum) is None
    ):
        raise DatasetError(f"malformed dataset format manifest: {path}")
    actual_size = artifact.stat().st_size
    if actual_size != recorded_size:
        raise DatasetError(f"dataset artifact size mismatch: {artifact}")
    if verify_checksum and stored_file_sha256(artifact) != recorded_checksum:
        raise DatasetError(f"dataset artifact checksum mismatch: {artifact}")
    return path, manifest


def command_text(command: Sequence[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in command)


def run_build(log_path: Path, rebuild: bool) -> dict[str, Any] | None:
    if GENERATOR.is_file() and not rebuild:
        if GENERATOR_BUILD_METADATA.is_file():
            metadata = read_json(GENERATOR_BUILD_METADATA)
            if metadata.get("binary_sha256") == sha256_file(GENERATOR) and isinstance(metadata.get("go_toolchain"), dict):
                return metadata["go_toolchain"]
        return None
    GENERATOR.parent.mkdir(parents=True, exist_ok=True)
    toolchain = resolve_go()
    command = [toolchain["binary"], "build", "-o", str(GENERATOR), "./cmd/tsbs_generate_data"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"# Go toolchain: {json.dumps(toolchain, sort_keys=True)}\n")
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
    save_json(
        GENERATOR_BUILD_METADATA,
        {"binary": "bin/tsbs_generate_data", "binary_sha256": sha256_file(GENERATOR), "built_at": utc_now(), "go_toolchain": toolchain},
    )
    return toolchain


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
    compression: str = "none",
    regenerate: bool,
    rebuild: bool,
) -> dict[str, Any]:
    validate_format(format_name)
    if compression not in COMPRESSIONS:
        raise DatasetError(f"unsupported compression: {compression}")
    expected_compression = manifest_compression(dataset_manifest)
    if compression != expected_compression:
        raise DatasetError(
            f"requested compression {compression} conflicts with dataset compression {expected_compression}"
        )
    path = variant_dir(dataset_dir, format_name)
    log_path = path / "generate.log"
    artifact = path / ("data.gz" if compression == "gzip" else "data")
    manifest_path = path / "manifest.json"
    if manifest_path.exists() and not regenerate:
        existing = read_json(manifest_path)
        if existing.get("status") == "completed":
            if rebuild:
                run_build(log_path, True)
            selected_dir, manifest = validate_variant(
                dataset_dir, format_name, verify_checksum=False
            )
            return result(dataset_dir, dataset_manifest, selected_dir, manifest, reused=True)

    path.mkdir(parents=True, exist_ok=True)
    toolchain = run_build(log_path, rebuild)
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
    sha256_command()
    compression_command = gzip_command() if compression == "gzip" else None
    return_code = 0
    compression_return_code = 0
    with log_path.open("a", encoding="utf-8") as log:
        header = f"$ {command_text(command)}\n"
        log.write(header)
        print(header, end="", file=sys.stderr)
        with temporary.open("wb") as output:
            process = subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE if compression_command else output,
                stderr=subprocess.PIPE,
            )
            assert process.stderr is not None

            def drain_stderr(stream: Any) -> None:
                for raw_line in iter(stream.readline, b""):
                    line = raw_line.decode("utf-8", errors="replace")
                    log.write(line)
                    log.flush()
                    sys.stderr.write(line)
                    sys.stderr.flush()

            stderr_thread = threading.Thread(target=drain_stderr, args=(process.stderr,), daemon=True)
            stderr_thread.start()
            compressor = None
            compression_stderr_thread = None
            if compression_command:
                assert process.stdout is not None
                compression_header = f"$ {command_text(compression_command)}\n"
                log.write(compression_header)
                print(compression_header, end="", file=sys.stderr)
                compressor = subprocess.Popen(
                    compression_command,
                    cwd=REPO_ROOT,
                    stdin=process.stdout,
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
                process.stdout.close()
                assert compressor.stderr is not None
                compression_stderr_thread = threading.Thread(
                    target=drain_stderr,
                    args=(compressor.stderr,),
                    daemon=True,
                )
                compression_stderr_thread.start()
            return_code = process.wait()
            stderr_thread.join()
            process.stderr.close()
            if compressor:
                compression_return_code = compressor.wait()
                assert compression_stderr_thread is not None
                compression_stderr_thread.join()
                assert compressor.stderr is not None
                compressor.stderr.close()
    if return_code or compression_return_code:
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
        if return_code:
            raise DatasetError(f"tsbs_generate_data failed with exit code {return_code}; see {log_path}")
        raise DatasetError(f"gzip failed with exit code {compression_return_code}; see {log_path}")

    try:
        stored_checksum = stored_file_sha256(temporary)
        stored_bytes = temporary.stat().st_size
    except (DatasetError, OSError):
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, artifact)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "format": format_name,
        "status": "completed",
        "artifact": artifact.name,
        "bytes": stored_bytes,
        "sha256": stored_checksum,
        "started_at": started_at,
        "finished_at": utc_now(),
        "log": "generate.log",
        "generator": {
            "binary": "bin/tsbs_generate_data",
            "binary_sha256": sha256_file(GENERATOR),
            "git_revision": git_revision(),
            **({"go_toolchain": toolchain} if toolchain else {}),
        },
    }
    save_json(manifest_path, manifest)
    return result(dataset_dir, dataset_manifest, path, manifest, reused=False)


def result(
    dataset_dir: Path,
    dataset_manifest: dict[str, Any],
    variant_dir: Path,
    variant_manifest: dict[str, Any],
    *,
    reused: bool,
) -> dict[str, Any]:
    format_name = str(variant_manifest["format"])
    return {
        "dataset_id": dataset_manifest["dataset_id"],
        "dataset_path": str(dataset_dir),
        "format": format_name,
        "compression": manifest_compression(dataset_manifest),
        "data_path": str(variant_dir / str(variant_manifest["artifact"])),
        "bytes": variant_manifest["bytes"],
        "sha256": variant_manifest["sha256"],
        "estimated_points": estimated_points(dataset_manifest["spec"]),
        "spec": dataset_manifest["spec"],
        "reused": reused,
    }


def logical_result(dataset_dir: Path, dataset_manifest: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    """Return metadata for a logical dataset without requiring a format artifact."""
    return {
        "dataset_id": dataset_manifest["dataset_id"],
        "dataset_path": str(dataset_dir),
        "compression": manifest_compression(dataset_manifest),
        "estimated_points": estimated_points(dataset_manifest["spec"]),
        "spec": dataset_manifest["spec"],
        "reused": reused,
    }


def prepare_dataset(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    explicit_selection = bool(args.dataset_id or args.dataset_path)
    dataset_dir = resolve_dataset_path(args) if explicit_selection else None
    if dataset_dir is not None and (dataset_dir / "dataset.json").exists():
        manifest = validate_dataset_manifest(dataset_dir)
        requested_compression = getattr(args, "compression", None)
        compression = manifest_compression(manifest)
        if requested_compression is not None and requested_compression != compression:
            raise DatasetError(
                f"requested compression {requested_compression} conflicts with dataset compression {compression}"
            )
        requested = logical_spec(args, manifest["spec"])
        if requested != manifest["spec"]:
            raise DatasetError(f"dataset settings do not match requested workload: {dataset_dir}")
        return dataset_dir, manifest

    spec = logical_spec(args)
    compression = getattr(args, "compression", None) or "none"
    dataset_dir = dataset_dir or resolve_dataset_path(args, spec, compression)
    manifest_path = dataset_dir / "dataset.json"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": args.dataset_id or automatic_dataset_id(spec, compression),
        "created_at": utc_now(),
        "compression": compression,
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
            compression = manifest_compression(manifest)
            variants = []
            if (path / "formats").is_dir():
                for child in sorted(path.joinpath("formats").iterdir()):
                    if not child.is_dir() or not (child / "manifest.json").is_file():
                        continue
                    try:
                        validate_variant(
                            path,
                            child.name,
                            verify_checksum=False,
                        )
                    except DatasetError:
                        continue
                    else:
                        variants.append({"format": child.name, "compression": compression})
            datasets.append(
                {
                    "dataset_id": manifest.get("dataset_id", path.name),
                    "dataset_path": str(path.resolve()),
                    "compression": compression,
                    "spec": manifest["spec"],
                    "formats": sorted({item["format"] for item in variants}),
                    "variants": variants,
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
    compression = manifest_compression(manifest)
    if args.compression is not None and args.compression != compression:
        raise DatasetError(
            f"requested compression {args.compression} conflicts with dataset compression {compression}"
        )
    selected: list[str] = []
    formats_dir = path / "formats"
    formats = [args.format] if args.format else (
        sorted(child.name for child in formats_dir.iterdir() if child.is_dir()) if formats_dir.exists() else []
    )
    for format_name in formats:
        if (variant_dir(path, format_name) / "manifest.json").is_file():
            selected.append(format_name)
    if not selected:
        raise DatasetError(f"dataset has no format variants: {path}")
    variants = []
    for format_name in selected:
        validate_format(format_name)
        selected_dir, variant = validate_variant(path, format_name, verify_checksum=True)
        variants.append(result(path, manifest, selected_dir, variant, reused=True))
    return {
        "dataset_id": manifest["dataset_id"],
        "dataset_path": str(path),
        "compression": compression,
        "variants": variants,
    }


def print_output(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True))
        return
    if isinstance(value, list):
        for item in value:
            print(f"{item['dataset_id']}\t{','.join(item['formats'])}\t{item['dataset_path']}")
    elif isinstance(value, dict) and "data_path" in value:
        action = "reused" if value.get("reused") else "generated"
        print(f"Dataset {action}: {value['dataset_id']} ({value['format']}, {value['compression']})")
        print(f"Data: {value['data_path']}")
        print(f"Stored file SHA-256: {value['sha256']}")
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
    generate.add_argument("--compression", choices=COMPRESSIONS)
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
    prepare.add_argument("--compression", choices=COMPRESSIONS)
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
    verify.add_argument("--compression", choices=COMPRESSIONS)
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
            compression = manifest_compression(manifest)
            point_count = estimated_points(manifest["spec"])
            if point_count is not None and point_count >= 50_000_000 and compression == "none":
                print(
                    f"warning: dataset has an estimated {point_count:,} points; consider --compression gzip",
                    file=sys.stderr,
                )
            output = generate_variant(
                path,
                manifest,
                args.format,
                compression=compression,
                regenerate=args.regenerate,
                rebuild=args.rebuild,
            )
        elif args.command == "prepare":
            selected_before = resolve_dataset_path(
                args, logical_spec(args), args.compression or "none"
            )
            existed = (selected_before / "dataset.json").is_file()
            path, manifest = prepare_dataset(args)
            output = logical_result(path, manifest, reused=existed)
        elif args.command == "list":
            output = list_datasets(args)
        elif args.command == "inspect":
            path, manifest = select_existing(args)
            output = {
                **manifest,
                "compression": manifest_compression(manifest),
                "dataset_path": str(path),
            }
        else:
            output = verify_dataset(args)
        if getattr(args, "result_file", None):
            save_json(args.result_file.resolve(), output)
        print_output(output, args.json)
        return 0
    except (DatasetError, TsbsEnvironmentError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
