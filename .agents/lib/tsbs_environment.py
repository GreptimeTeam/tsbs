"""Resolve and manage the Go toolchain used by TSBS automation."""

from __future__ import annotations

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
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INSTALL_ROOT = REPO_ROOT / ".benchmarks" / "environment" / "go"
MINIMUM_GO_VERSION = (1, 21, 0)
MANAGED_GO_VERSION = "1.21.13"
DOWNLOADS_API = "https://go.dev/dl/?mode=json&include=all"
DOWNLOAD_BASE_URL = "https://go.dev/dl"
USER_AGENT = "tsbs-environment-setup/1"
SCHEMA_VERSION = 1
GO_VERSION_RE = re.compile(r"^go version go(\d+)\.(\d+)(?:\.(\d+))? (\S+)$")


class TsbsEnvironmentError(RuntimeError):
    """Raised when a suitable TSBS build environment cannot be prepared."""


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
        raise TsbsEnvironmentError(f"missing Go installation manifest: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TsbsEnvironmentError(f"invalid Go installation manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TsbsEnvironmentError(f"Go installation manifest must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            raise TsbsEnvironmentError(f"unsupported Go installation entry: {entry}")
        digest.update(f"{kind}\0{relative}\0{mode:o}\0".encode())
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


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
    raise TsbsEnvironmentError(f"unsupported Go platform: {system} {machine}")


def parse_go_version(output: str) -> tuple[tuple[int, int, int], str]:
    match = GO_VERSION_RE.fullmatch(output.strip())
    if not match:
        raise TsbsEnvironmentError(f"could not parse stable Go version output: {output.strip() or '<empty>'}")
    version = tuple(int(value or 0) for value in match.group(1, 2, 3))
    return version, ".".join(str(value) for value in version)


def probe_go(binary: Path, *, minimum: tuple[int, int, int] = MINIMUM_GO_VERSION) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True, check=False, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TsbsEnvironmentError(f"could not execute Go binary {binary}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise TsbsEnvironmentError(f"Go binary {binary} failed version verification: {detail}")
    version, normalized = parse_go_version(result.stdout)
    if version < minimum:
        required = ".".join(str(value) for value in minimum[:2])
        raise TsbsEnvironmentError(f"Go {normalized} is too old; TSBS requires Go {required} or newer")
    resolved = binary.expanduser().resolve()
    return {
        "source": "system",
        "version": normalized,
        "platform": platform_tag(),
        "binary": str(resolved),
        "binary_sha256": sha256_file(resolved),
    }


def installation_path(install_root: Path | None = None) -> Path:
    root = (install_root or DEFAULT_INSTALL_ROOT).expanduser().resolve()
    return root / MANAGED_GO_VERSION / platform_tag()


def request(url: str, *, accept: str = "application/octet-stream,*/*") -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})


def download_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(request(url, accept="application/json"), timeout=60) as response:
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise TsbsEnvironmentError(f"could not download {url}: {exc}") from exc


def download(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(request(url), timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (OSError, urllib.error.URLError) as exc:
        raise TsbsEnvironmentError(f"could not download {url}: {exc}") from exc


def release_metadata(target: str) -> dict[str, str]:
    try:
        releases = json.loads(download_bytes(DOWNLOADS_API))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TsbsEnvironmentError("could not parse official Go download metadata") from exc
    system, architecture = target.split("_", 1)
    expected_version = f"go{MANAGED_GO_VERSION}"
    if not isinstance(releases, list):
        raise TsbsEnvironmentError("official Go download metadata is malformed")
    for release in releases:
        if not isinstance(release, dict) or release.get("version") != expected_version:
            continue
        for artifact in release.get("files", []):
            if not isinstance(artifact, dict):
                continue
            if artifact.get("os") == system and artifact.get("arch") == architecture and artifact.get("kind") == "archive":
                filename, checksum = artifact.get("filename"), artifact.get("sha256")
                if isinstance(filename, str) and isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum):
                    return {
                        "filename": filename,
                        "sha256": checksum,
                        "source_url": f"{DOWNLOAD_BASE_URL}/{filename}",
                    }
    raise TsbsEnvironmentError(f"official Go metadata has no {expected_version} archive for {target}")


def safe_extract_go(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or not relative.parts or relative.parts[0] != "go" or ".." in relative.parts:
                raise TsbsEnvironmentError(f"unsafe path in Go archive: {member.name}")
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise TsbsEnvironmentError(f"unsupported entry in Go archive: {member.name}")
        bundle.extractall(destination, members=members)
    extracted = destination / "go"
    binary = extracted / "bin" / "go"
    if not binary.is_file():
        raise TsbsEnvironmentError("Go archive does not contain bin/go")
    return extracted


@contextlib.contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".install.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def validate_installation(path: Path) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    required = {
        "schema_version", "kind", "version", "platform", "source_url", "archive_sha256",
        "binary", "binary_sha256", "distribution_sha256", "created_at",
    }
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != "tsbs-go-installation" or not required.issubset(manifest):
        raise TsbsEnvironmentError(f"malformed Go installation manifest: {path / 'manifest.json'}")
    if manifest["version"] != MANAGED_GO_VERSION or manifest["platform"] != platform_tag():
        raise TsbsEnvironmentError(f"Go installation identity mismatch: {path}")
    binary = path / str(manifest["binary"])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise TsbsEnvironmentError(f"managed Go binary is missing or not executable: {binary}")
    if sha256_file(binary) != manifest["binary_sha256"]:
        raise TsbsEnvironmentError(f"managed Go binary checksum mismatch: {binary}")
    if distribution_sha256(path) != manifest["distribution_sha256"]:
        raise TsbsEnvironmentError(f"managed Go distribution checksum mismatch: {path}")
    checked = probe_go(binary)
    if checked["version"] != MANAGED_GO_VERSION:
        raise TsbsEnvironmentError(f"managed Go reports {checked['version']}, expected {MANAGED_GO_VERSION}")
    return {
        **manifest,
        "source": "managed",
        "binary": str(binary.resolve()),
        "installation_path": str(path.resolve()),
    }


def install_go(install_root: Path | None = None) -> dict[str, Any]:
    root = (install_root or DEFAULT_INSTALL_ROOT).expanduser().resolve()
    destination = installation_path(root)
    with installation_lock(root):
        if destination.exists():
            return {**validate_installation(destination), "reused": True}
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = platform_tag()
        metadata = release_metadata(target)
        temporary = Path(tempfile.mkdtemp(prefix=f".go-{MANAGED_GO_VERSION}-", dir=destination.parent))
        try:
            archive = temporary / metadata["filename"]
            download(metadata["source_url"], archive)
            actual = sha256_file(archive)
            if actual != metadata["sha256"]:
                raise TsbsEnvironmentError(f"Go archive checksum mismatch: expected {metadata['sha256']}, got {actual}")
            extracted = safe_extract_go(archive, temporary)
            archive.unlink()
            binary = extracted / "bin" / "go"
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "kind": "tsbs-go-installation",
                "version": MANAGED_GO_VERSION,
                "platform": target,
                "created_at": utc_now(),
                "source_url": metadata["source_url"],
                "archive_sha256": actual,
                "binary": "bin/go",
                "binary_sha256": sha256_file(binary),
                "distribution_sha256": distribution_sha256(extracted),
            }
            save_json(extracted / "manifest.json", manifest)
            validate_installation(extracted)
            os.replace(extracted, destination)
            return {**validate_installation(destination), "reused": False}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def resolve_go(install_root: Path | None = None, system_go: str | Path | None = None) -> dict[str, Any]:
    candidate = str(system_go) if system_go is not None else shutil.which("go")
    if candidate:
        try:
            return {**probe_go(Path(candidate)), "reused": True}
        except TsbsEnvironmentError:
            pass
    return install_go(install_root)


def verify_go(install_root: Path | None = None, system_go: str | Path | None = None) -> dict[str, Any]:
    candidate = str(system_go) if system_go is not None else shutil.which("go")
    if candidate:
        try:
            return {**probe_go(Path(candidate)), "reused": True}
        except TsbsEnvironmentError:
            pass
    path = installation_path(install_root)
    if not path.exists():
        raise TsbsEnvironmentError("no suitable Go 1.21+ toolchain is installed; run prepare")
    return {**validate_installation(path), "reused": True}
