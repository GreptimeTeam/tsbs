#!/usr/bin/env python3
"""Install GreptimeDB releases and prepare reusable benchmark workspaces."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_ROOT = REPO_ROOT / ".benchmarks" / "greptimedb"
DEFAULT_INSTALL_ROOT = DEFAULT_ROOT / "installations"
DEFAULT_DATABASE_ROOT = DEFAULT_ROOT / "databases"
GITHUB_API_LATEST = "https://api.github.com/repos/GreptimeTeam/greptimedb/releases/latest"
RELEASE_BASE_URL = "https://github.com/GreptimeTeam/greptimedb/releases/download"
USER_AGENT = "tsbs-greptimedb-setup/1.0"
SCHEMA_VERSION = 1
INSTALLATION_SCHEMA_VERSION = 1
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


def normalize_version(value: str) -> str:
    normalized = value[1:] if value.startswith("v") else value
    if not VERSION_RE.fullmatch(normalized):
        raise SetupError("--version must be an exact semantic version such as 1.1.4 or v1.2.0-beta.1")
    return normalized


def platform_tag(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return "linux_amd64"
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return "linux_arm64"
    if system == "Darwin" and machine in ("x86_64", "amd64"):
        return "darwin_amd64"
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "darwin_arm64"
    raise SetupError(f"unsupported native platform: {system} {machine}")


def artifact_name(version: str, target: str) -> str:
    system, architecture = target.split("_", 1)
    return f"greptime-{system}-{architecture}-v{version}.tar.gz"


def request(url: str, *, accept: str = "application/octet-stream,*/*") -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def download_bytes(url: str, *, accept: str = "application/octet-stream,*/*") -> bytes:
    try:
        with urllib.request.urlopen(request(url, accept=accept), timeout=60) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"could not download {url}: {exc}") from exc


def download(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(request(url), timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as exc:
        raise SetupError(f"could not download {url}: {exc}") from exc


def resolve_official_version() -> str:
    try:
        release = json.loads(download_bytes(GITHUB_API_LATEST, accept="application/vnd.github+json"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SetupError("could not parse the latest stable GreptimeDB release; provide an exact --version") from exc
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise SetupError("GitHub did not return a stable GreptimeDB release; provide an exact --version")
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        raise SetupError("latest stable GreptimeDB release has no tag; provide an exact --version")
    try:
        return normalize_version(tag)
    except SetupError as exc:
        raise SetupError("latest stable GreptimeDB release has an invalid tag; provide an exact --version") from exc


def resolve_args_version(args: argparse.Namespace) -> None:
    if args.version is None:
        args.version = resolve_official_version()
        args.version_source = "github-latest-stable"
    else:
        args.version = normalize_version(args.version)
        args.version_source = "explicit"


def installation_path(args: argparse.Namespace, target: str | None = None) -> Path:
    root = (args.install_root or DEFAULT_INSTALL_ROOT).expanduser().resolve()
    return root / args.version / (target or platform_tag())


def distribution_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    entries = sorted(
        (entry for entry in path.rglob("*") if entry.relative_to(path).as_posix() != "manifest.json"),
        key=lambda entry: entry.relative_to(path).as_posix(),
    )
    for entry in entries:
        relative = entry.relative_to(path).as_posix()
        stat = entry.lstat()
        mode = stat.st_mode & 0o7777
        if entry.is_symlink():
            kind, content = "symlink", os.readlink(entry).encode()
        elif entry.is_file():
            kind, content = "file", bytes.fromhex(sha256_file(entry))
        elif entry.is_dir():
            kind, content = "directory", b""
        else:
            raise SetupError(f"unsupported installation entry: {entry}")
        digest.update(f"{kind}\0{relative}\0{mode:o}\0".encode())
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def verify_binary(binary: Path, version: str) -> None:
    try:
        result = subprocess.run(
            [str(binary), "--version"], cwd=binary.parent, capture_output=True,
            text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"GreptimeDB installation is not runnable: {binary}; reinstall with --reinstall: {exc}") from exc
    tokens = re.split(r"\s+", f"{result.stdout}\n{result.stderr}".strip())
    matches = [token.lstrip("v") for token in tokens if token.lstrip("v") == version or token.lstrip("v").startswith(version + "-")]
    if result.returncode != 0 or not matches:
        raise SetupError(f"GreptimeDB installation failed version validation for {version}; reinstall with --reinstall")


def validate_installation(path: Path, version: str | None = None) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    required = {
        "schema_version", "kind", "version", "version_source", "platform", "binary",
        "binary_sha256", "archive_sha256", "distribution_sha256",
    }
    if manifest.get("schema_version") != INSTALLATION_SCHEMA_VERSION or manifest.get("kind") != "greptimedb-installation" or not required.issubset(manifest):
        raise SetupError(f"malformed installation manifest: {path / 'manifest.json'}")
    if version and manifest["version"] != version:
        raise SetupError("installation version mismatch")
    binary = path / manifest["binary"]
    if not binary.is_file() or not os.access(binary, os.X_OK) or sha256_file(binary) != manifest["binary_sha256"]:
        raise SetupError(f"installation binary checksum mismatch: {binary}")
    if distribution_sha256(path) != manifest["distribution_sha256"]:
        raise SetupError(f"installation distribution checksum mismatch: {path}")
    verify_binary(binary, manifest["version"])
    return manifest


def safe_extract_distribution(archive: Path, destination: Path, expected_root: str) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise SetupError("archive is empty")
        validated: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
        for member in members:
            source = PurePosixPath(member.name)
            if source.is_absolute() or not source.parts or source.parts[0] != expected_root:
                raise SetupError("archive must contain exactly the expected top-level directory")
            relative = PurePosixPath(*source.parts[1:])
            if any(part in ("", ".", "..") for part in relative.parts):
                raise SetupError(f"unsafe archive path: {member.name}")
            if member.islnk() or member.isdev() or member.isfifo() or not (member.isdir() or member.isfile() or member.issym()):
                raise SetupError(f"unsupported archive member: {member.name}")
            if member.issym():
                link = PurePosixPath(member.linkname)
                if link.is_absolute():
                    raise SetupError(f"unsafe archive symlink: {member.name}")
                resolved = list(relative.parent.parts)
                for part in link.parts:
                    if part in ("", "."):
                        continue
                    if part == "..":
                        if not resolved:
                            raise SetupError(f"archive symlink escapes distribution: {member.name}")
                        resolved.pop()
                    else:
                        resolved.append(part)
            validated.append((member, relative))
        for member, relative in validated:
            if relative.parts and member.isdir():
                target = destination.joinpath(*relative.parts); target.mkdir(parents=True, exist_ok=True); target.chmod(member.mode & 0o7777)
        for member, relative in validated:
            if relative.parts and member.isfile():
                target = destination.joinpath(*relative.parts); target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise SetupError(f"could not read archive member: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o7777)
        for member, relative in validated:
            if relative.parts and member.issym():
                target = destination.joinpath(*relative.parts); target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(member.linkname, target)
    binary = destination / "greptime"
    if not binary.is_file():
        raise SetupError("archive does not contain the greptime binary")
    return binary


def install(args: argparse.Namespace) -> dict[str, Any]:
    target = platform_tag(); destination = installation_path(args, target)
    if destination.exists() and not args.reinstall:
        manifest = validate_installation(destination, args.version)
        return {**manifest, "installation_path": str(destination), "reused": True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    name = artifact_name(args.version, target); tag = f"v{args.version}"
    temporary = Path(tempfile.mkdtemp(prefix=f".{tag}-", dir=destination.parent))
    try:
        archive = temporary / name; checksum_file = temporary / f"{name[:-7]}.sha256sum"
        source_url = f"{RELEASE_BASE_URL}/{tag}/{name}"
        download(source_url, archive); download(f"{RELEASE_BASE_URL}/{tag}/{checksum_file.name}", checksum_file)
        checksum_match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_file.read_text(encoding="utf-8"))
        if not checksum_match:
            raise SetupError("vendor checksum file is malformed")
        expected, actual = checksum_match.group(1).lower(), sha256_file(archive)
        if actual != expected:
            raise SetupError(f"archive checksum mismatch: expected {expected}, got {actual}")
        expected_root = name[:-7]
        binary = safe_extract_distribution(archive, temporary, expected_root)
        archive.unlink(); checksum_file.unlink()
        manifest = {
            "schema_version": INSTALLATION_SCHEMA_VERSION, "kind": "greptimedb-installation",
            "version": args.version, "version_source": args.version_source, "platform": target,
            "created_at": utc_now(), "source_url": source_url, "archive_sha256": actual,
            "binary": binary.relative_to(temporary).as_posix(), "binary_sha256": sha256_file(binary),
            "distribution_sha256": distribution_sha256(temporary),
        }
        save_json(temporary / "manifest.json", manifest)
        validate_installation(temporary, args.version)
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


def database_path(args: argparse.Namespace) -> Path:
    return (args.database_root or DEFAULT_DATABASE_ROOT).expanduser().resolve() / args.database_id


@contextlib.contextmanager
def lock_database(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError(f"managed database workspace is locked: {path}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def copy_tree_stats(path: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for entry in path.rglob("*"):
        if entry.is_symlink():
            raise SetupError(f"database data directory contains unsupported symlink: {entry}")
        if entry.is_file():
            files += 1
            size += entry.stat().st_size
        elif not entry.is_dir():
            raise SetupError(f"database data directory contains unsupported entry: {entry}")
    return files, size


def validate_database(path: Path, expected_id: str | None = None) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    required = {
        "schema_version", "kind", "database_id", "version", "version_source", "platform",
        "installation_path", "binary_sha256", "created_at", "database", "binding",
    }
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "greptimedb-database" or not required.issubset(manifest):
        raise SetupError(f"malformed or legacy database manifest: {path / 'manifest.json'}")
    if expected_id and manifest["database_id"] != expected_id:
        raise SetupError("database identity mismatch")
    installation = Path(manifest["installation_path"])
    installed = validate_installation(installation, manifest["version"])
    if installed["platform"] != manifest["platform"] or installed["binary_sha256"] != manifest["binary_sha256"]:
        raise SetupError("database installation identity mismatch")
    return manifest


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    installation = installation_path(args); installed = validate_installation(installation, args.version)
    path = database_path(args); manifest_path = path / "manifest.json"
    if manifest_path.exists():
        manifest = validate_database(path, args.database_id)
        expected = (args.version, installed["platform"], installed["binary_sha256"], args.database)
        actual = (manifest["version"], manifest["platform"], manifest["binary_sha256"], manifest["database"])
        if actual != expected:
            raise SetupError("database is already bound to another version, binary, platform, or SQL database")
        return {**manifest, "database_path": str(path), "reused": True}
    if path.exists() and any(path.iterdir()):
        raise SetupError(f"database workspace exists without a setup-compatible manifest: {path}")
    path.mkdir(parents=True, exist_ok=True); (path / "data").mkdir(); (path / "logs").mkdir()
    manifest = {
        "schema_version": SCHEMA_VERSION, "kind": "greptimedb-database", "database_id": args.database_id,
        "version": args.version, "version_source": args.version_source, "platform": installed["platform"],
        "installation_path": str(installation), "binary_sha256": installed["binary_sha256"],
        "created_at": utc_now(), "updated_at": utc_now(), "database": args.database, "binding": None,
    }
    save_json(manifest_path, manifest)
    return {**manifest, "database_path": str(path), "reused": False}


def copy_database(args: argparse.Namespace) -> dict[str, Any]:
    root = (args.database_root or DEFAULT_DATABASE_ROOT).expanduser().resolve()
    source = root / args.source_database_id
    destination = root / args.database_id
    if source == destination:
        raise SetupError("source and destination database IDs must differ")
    if destination.exists():
        raise SetupError(f"destination database workspace already exists: {destination}")

    installed = validate_installation(installation_path(args), args.version)

    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{args.database_id}.copy-", dir=root))
    try:
        with lock_database(source):
            source_manifest = validate_database(source, args.source_database_id)
            if source_manifest["binding"] is None:
                raise SetupError("source database workspace has no loaded dataset binding")
            if installed["platform"] != source_manifest["platform"]:
                raise SetupError("target installation platform differs from the source database platform")
            source_data = source / "data"
            if not source_data.is_dir():
                raise SetupError(f"source database data directory does not exist: {source_data}")
            file_count, byte_count = copy_tree_stats(source_data)
            shutil.copytree(source_data, temporary / "data", copy_function=shutil.copy2)
            copied_files, copied_bytes = copy_tree_stats(temporary / "data")
            if (copied_files, copied_bytes) != (file_count, byte_count):
                raise SetupError("copied database data does not match source file count and size")
            (temporary / "logs").mkdir()
            copied_at = utc_now()
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "kind": "greptimedb-database",
                "database_id": args.database_id,
                "version": args.version,
                "version_source": args.version_source,
                "platform": installed["platform"],
                "installation_path": str(installation_path(args)),
                "binary_sha256": installed["binary_sha256"],
                "created_at": copied_at,
                "updated_at": copied_at,
                "database": source_manifest["database"],
                "binding": source_manifest["binding"],
                "copied_from": {
                    "database_id": source_manifest["database_id"],
                    "database_path": str(source),
                    "version": source_manifest["version"],
                    "binary_sha256": source_manifest["binary_sha256"],
                    "manifest_sha256": sha256_file(source / "manifest.json"),
                    "copied_at": copied_at,
                    "method": "full",
                    "files": copied_files,
                    "bytes": copied_bytes,
                },
            }
            save_json(temporary / "manifest.json", manifest)
            if destination.exists():
                raise SetupError(f"destination database workspace already exists: {destination}")
            os.replace(temporary, destination)
        return {**manifest, "database_path": str(destination), "reused": False}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def print_value(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install"); install_parser.add_argument("--version"); install_parser.add_argument("--install-root", type=Path); install_parser.add_argument("--reinstall", action="store_true")
    prepare_parser = sub.add_parser("prepare"); prepare_parser.add_argument("--database-id", required=True); prepare_parser.add_argument("--version"); prepare_parser.add_argument("--database", default="benchmark"); prepare_parser.add_argument("--install-root", type=Path); prepare_parser.add_argument("--database-root", type=Path)
    copy_parser = sub.add_parser("copy"); copy_parser.add_argument("--source-database-id", required=True); copy_parser.add_argument("--database-id", required=True); copy_parser.add_argument("--version", required=True); copy_parser.add_argument("--install-root", type=Path); copy_parser.add_argument("--database-root", type=Path)
    list_parser = sub.add_parser("list"); list_parser.add_argument("--database-root", type=Path)
    inspect_parser = sub.add_parser("inspect"); inspect_parser.add_argument("--database-id", required=True); inspect_parser.add_argument("--database-root", type=Path)
    verify_parser = sub.add_parser("verify"); verify_parser.add_argument("--database-id", required=True); verify_parser.add_argument("--database-root", type=Path)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if hasattr(args, "version") and args.version is not None:
        normalize_version(args.version)
    if hasattr(args, "database_id") and not ID_RE.fullmatch(args.database_id):
        raise SetupError("--database-id contains invalid characters")
    if hasattr(args, "source_database_id") and not ID_RE.fullmatch(args.source_database_id):
        raise SetupError("--source-database-id contains invalid characters")
    if hasattr(args, "database") and (not args.database or "\x00" in args.database):
        raise SetupError("--database must not be empty or contain NUL")


def main(argv: Sequence[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        validate_args(args)
        if args.command in ("install", "prepare", "copy"):
            resolve_args_version(args)
        if args.command == "install":
            value = install(args)
        elif args.command == "prepare":
            value = prepare(args)
        elif args.command == "copy":
            value = copy_database(args)
        elif args.command in ("inspect", "verify"):
            path = database_path(args); value = {**validate_database(path, args.database_id), "database_path": str(path)}
        else:
            root = (args.database_root or DEFAULT_DATABASE_ROOT).expanduser().resolve(); value = []
            if root.exists():
                for path in sorted(root.iterdir()):
                    if path.is_dir():
                        try:
                            value.append({**validate_database(path), "database_path": str(path)})
                        except SetupError:
                            continue
        print_value(value); return 0
    except (SetupError, OSError, tarfile.TarError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
