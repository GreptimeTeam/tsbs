#!/usr/bin/env python3
"""Install and prepare reusable local InfluxDB 3 instances."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_ROOT = REPO_ROOT / ".benchmarks" / "influxdb3"
DEFAULT_INSTALL_ROOT = DEFAULT_ROOT / "installations"
DEFAULT_INSTANCE_ROOT = DEFAULT_ROOT / "instances"
BASE_URL = "https://dl.influxdata.com/influxdb/releases"
SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


class SetupError(RuntimeError):
    """Raised for an actionable setup failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SetupError(f"missing manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SetupError(f"invalid JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"manifest must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def platform_tag(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux_amd64"
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return "linux_arm64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "darwin_arm64"
    raise SetupError(f"unsupported native platform: {system} {machine}")


def artifact_name(edition: str, version: str, target: str) -> str:
    return f"influxdb3-{edition}-{version}_{target}.tar.gz"


def installation_path(args: argparse.Namespace, target: str | None = None) -> Path:
    root = (args.install_root or DEFAULT_INSTALL_ROOT).expanduser().resolve()
    return root / args.edition / args.version / (target or platform_tag())


def validate_installation(path: Path, edition: str | None = None, version: str | None = None) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    required = {"schema_version", "kind", "edition", "version", "platform", "binary", "binary_sha256", "archive_sha256"}
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "influxdb3-installation" or not required.issubset(manifest):
        raise SetupError(f"malformed installation manifest: {path / 'manifest.json'}")
    if edition and manifest["edition"] != edition:
        raise SetupError("installation edition mismatch")
    if version and manifest["version"] != version:
        raise SetupError("installation version mismatch")
    binary = path / manifest["binary"]
    if not binary.is_file() or sha256_file(binary) != manifest["binary_sha256"]:
        raise SetupError(f"installation binary checksum mismatch: {binary}")
    return manifest


def download(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"could not download {url}: {exc}") from exc


def safe_extract_binary(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = [member for member in bundle.getmembers() if Path(member.name).name == "influxdb3" and member.isfile()]
        if len(members) != 1:
            raise SetupError("archive must contain exactly one influxdb3 binary")
        member = members[0]
        source = bundle.extractfile(member)
        if source is None:
            raise SetupError("could not read influxdb3 binary from archive")
        binary = destination / "influxdb3"
        with binary.open("wb") as output:
            shutil.copyfileobj(source, output)
        binary.chmod(0o755)
        return binary


def install(args: argparse.Namespace) -> dict[str, Any]:
    target = platform_tag()
    destination = installation_path(args, target)
    if destination.exists() and not args.reinstall:
        manifest = validate_installation(destination, args.edition, args.version)
        return {**manifest, "installation_path": str(destination), "reused": True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    name = artifact_name(args.edition, args.version, target)
    temporary = Path(tempfile.mkdtemp(prefix=f".{args.edition}-{args.version}-", dir=destination.parent))
    try:
        archive = temporary / name
        checksum_file = temporary / f"{name}.sha256"
        download(f"{BASE_URL}/{name}", archive)
        download(f"{BASE_URL}/{name}.sha256", checksum_file)
        checksum_match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_file.read_text(encoding="utf-8"))
        if not checksum_match:
            raise SetupError("vendor checksum file is malformed")
        expected = checksum_match.group(1).lower()
        actual = sha256_file(archive)
        if actual != expected:
            raise SetupError(f"archive checksum mismatch: expected {expected}, got {actual}")
        binary = safe_extract_binary(archive, temporary)
        manifest = {
            "schema_version": SCHEMA_VERSION, "kind": "influxdb3-installation",
            "edition": args.edition, "version": args.version, "platform": target,
            "created_at": utc_now(), "source_url": f"{BASE_URL}/{name}",
            "archive_sha256": actual, "binary": binary.name,
            "binary_sha256": sha256_file(binary),
        }
        archive.unlink(); checksum_file.unlink(); save_json(temporary / "manifest.json", manifest)
        validate_installation(temporary, args.edition, args.version)
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.old-{os.getpid()}")
            os.replace(destination, backup)
            try:
                os.replace(temporary, destination)
            except Exception:
                os.replace(backup, destination)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(temporary, destination)
        return {**manifest, "installation_path": str(destination), "reused": False}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def instance_path(args: argparse.Namespace) -> Path:
    return (args.instance_root or DEFAULT_INSTANCE_ROOT).expanduser().resolve() / args.instance_id


def validate_instance(path: Path, expected_id: str | None = None) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    required = {"schema_version", "kind", "instance_id", "edition", "version", "installation_path", "binary_sha256", "node_id", "cluster_id", "license", "database", "binding"}
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "influxdb3-instance" or not required.issubset(manifest):
        raise SetupError(f"malformed instance manifest: {path / 'manifest.json'}")
    if expected_id and manifest["instance_id"] != expected_id:
        raise SetupError("instance identity mismatch")
    installation = Path(manifest["installation_path"])
    installed = validate_installation(installation, manifest["edition"], manifest["version"])
    if installed["binary_sha256"] != manifest["binary_sha256"]:
        raise SetupError("instance installation checksum mismatch")
    return manifest


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    installation = installation_path(args)
    installed = validate_installation(installation, args.edition, args.version)
    path = instance_path(args)
    if (path / "manifest.json").exists():
        manifest = validate_instance(path, args.instance_id)
        identity = (manifest["edition"], manifest["version"], manifest["binary_sha256"])
        expected = (args.edition, args.version, installed["binary_sha256"])
        if identity != expected:
            raise SetupError("instance is already bound to another edition, version, or binary")
        return {**manifest, "instance_path": str(path), "reused": True}
    path.mkdir(parents=True, exist_ok=True)
    (path / "data").mkdir(); (path / "logs").mkdir()
    stem = re.sub(r"[^A-Za-z0-9-]", "-", args.instance_id)
    manifest = {
        "schema_version": SCHEMA_VERSION, "kind": "influxdb3-instance",
        "instance_id": args.instance_id, "edition": args.edition, "version": args.version,
        "installation_path": str(installation), "binary_sha256": installed["binary_sha256"],
        "node_id": f"{stem}-node", "cluster_id": f"{stem}-cluster" if args.edition == "enterprise" else None,
        "created_at": utc_now(), "updated_at": utc_now(), "license": {"status": "not-required" if args.edition == "core" else "unconfigured", "source": None},
        "database": None, "binding": None,
    }
    save_json(path / "manifest.json", manifest)
    return {**manifest, "instance_path": str(path), "reused": False}


def wait_health(url: str, process: subprocess.Popen[Any], timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    return False


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port)); return True
        except OSError:
            return False


def activate(args: argparse.Namespace) -> dict[str, Any]:
    path = instance_path(args); manifest = validate_instance(path, args.instance_id)
    if manifest["edition"] != "enterprise":
        raise SetupError("license activation is only valid for Enterprise instances")
    if not port_available(args.http_port):
        raise SetupError(f"HTTP port {args.http_port} is unavailable")
    installation = Path(manifest["installation_path"]); binary = installation / "influxdb3"
    command = [str(binary), "serve", "--object-store=file", f"--data-dir={path / 'data'}", f"--node-id={manifest['node_id']}", f"--cluster-id={manifest['cluster_id']}", f"--http-bind=127.0.0.1:{args.http_port}", "--without-auth"]
    env = os.environ.copy(); source: str
    if args.license_file:
        license_file = args.license_file.expanduser().resolve()
        if not license_file.is_file():
            raise SetupError(f"license file does not exist: {license_file}")
        command.append(f"--license-file={license_file}"); source = "file"
    else:
        license_email = os.environ[args.license_email_env]
        env["INFLUXDB3_LICENSE_EMAIL"] = license_email
        env["INFLUXDB3_LICENSE_TYPE"] = args.license_type
        source = args.license_type
    log_path = path / "logs" / "license-activation.log"
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\nActivation attempt at {utc_now()} (source={source})\n"); log.flush()
        process = subprocess.Popen(command, cwd=path, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        ready = False
        try:
            ready = wait_health(f"http://127.0.0.1:{args.http_port}", process, args.activation_timeout)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
    if args.license_type:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if license_email in text:
            log_path.write_text(text.replace(license_email, "<redacted-email>"), encoding="utf-8")
    manifest["license"] = {
        "status": "active" if ready else "pending", "source": source,
        "path": str(license_file) if args.license_file else None,
    }
    manifest["updated_at"] = utc_now(); save_json(path / "manifest.json", manifest)
    if not ready:
        raise SetupError(f"Enterprise was not ready within {args.activation_timeout}s; verify the email if required and see {log_path}")
    return {**manifest, "instance_path": str(path)}


def print_value(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install"); install_parser.add_argument("--edition", choices=("core", "enterprise"), required=True); install_parser.add_argument("--version", required=True); install_parser.add_argument("--install-root", type=Path); install_parser.add_argument("--reinstall", action="store_true")
    prepare_parser = sub.add_parser("prepare"); prepare_parser.add_argument("--instance-id", required=True); prepare_parser.add_argument("--edition", choices=("core", "enterprise"), required=True); prepare_parser.add_argument("--version", required=True); prepare_parser.add_argument("--install-root", type=Path); prepare_parser.add_argument("--instance-root", type=Path)
    activate_parser = sub.add_parser("activate"); activate_parser.add_argument("--instance-id", required=True); activate_parser.add_argument("--instance-root", type=Path); license_group = activate_parser.add_mutually_exclusive_group(required=True); license_group.add_argument("--license-file", type=Path); license_group.add_argument("--license-type", choices=("trial", "home")); activate_parser.add_argument("--license-email-env", default="INFLUXDB3_LICENSE_EMAIL"); activate_parser.add_argument("--http-port", type=int, default=8181); activate_parser.add_argument("--activation-timeout", type=int, default=600)
    list_parser = sub.add_parser("list"); list_parser.add_argument("--instance-root", type=Path)
    inspect_parser = sub.add_parser("inspect"); inspect_parser.add_argument("--instance-id", required=True); inspect_parser.add_argument("--instance-root", type=Path)
    verify_parser = sub.add_parser("verify"); verify_parser.add_argument("--instance-id", required=True); verify_parser.add_argument("--instance-root", type=Path)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "version") and not VERSION_RE.fullmatch(args.version):
        raise SetupError("--version must be an exact semantic version such as 3.11.1")
    if hasattr(args, "instance_id") and not ID_RE.fullmatch(args.instance_id):
        raise SetupError("--instance-id contains invalid characters")
    if args.command == "activate":
        if args.license_type and not os.environ.get(args.license_email_env):
            raise SetupError(f"trial/home activation requires email in ${args.license_email_env}")
        if args.activation_timeout <= 0 or not 1 <= args.http_port <= 65535:
            raise SetupError("activation timeout and HTTP port must be positive and valid")


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        validate_args(args)
        if args.command == "install": value = install(args)
        elif args.command == "prepare": value = prepare(args)
        elif args.command == "activate": value = activate(args)
        elif args.command in ("inspect", "verify"):
            path = instance_path(args); value = {**validate_instance(path, args.instance_id), "instance_path": str(path)}
        else:
            root = (args.instance_root or DEFAULT_INSTANCE_ROOT).expanduser().resolve(); value = []
            if root.exists():
                for path in sorted(root.iterdir()):
                    if path.is_dir():
                        try: value.append({**validate_instance(path), "instance_path": str(path)})
                        except SetupError: continue
        print_value(value); return 0
    except (SetupError, OSError, tarfile.TarError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
