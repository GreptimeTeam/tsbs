from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


LIB = Path(__file__).resolve().parents[3] / "lib"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(SCRIPTS))

import setup  # noqa: E402
import tsbs_environment as environment  # noqa: E402


class PlatformAndVersionTests(unittest.TestCase):
    def test_supported_platforms(self) -> None:
        self.assertEqual(environment.platform_tag("Linux", "x86_64"), "linux_amd64")
        self.assertEqual(environment.platform_tag("Linux", "aarch64"), "linux_arm64")
        self.assertEqual(environment.platform_tag("Darwin", "x86_64"), "darwin_amd64")
        self.assertEqual(environment.platform_tag("Darwin", "arm64"), "darwin_arm64")
        with self.assertRaises(environment.TsbsEnvironmentError):
            environment.platform_tag("Windows", "AMD64")

    def test_stable_version_parsing(self) -> None:
        self.assertEqual(environment.parse_go_version("go version go1.21.0 linux/amd64")[0], (1, 21, 0))
        self.assertEqual(environment.parse_go_version("go version go1.24 darwin/arm64")[0], (1, 24, 0))
        for output in ("go version devel go1.25-abc linux/amd64", "go1.21.0"):
            with self.subTest(output=output), self.assertRaises(environment.TsbsEnvironmentError):
                environment.parse_go_version(output)

    def test_probe_rejects_old_go(self) -> None:
        completed = mock.Mock(returncode=0, stdout="go version go1.20.14 linux/amd64\n", stderr="")
        with mock.patch.object(environment.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(environment.TsbsEnvironmentError, "too old"):
                environment.probe_go(Path("/tmp/go"))


class ResolutionTests(unittest.TestCase):
    def test_reuses_acceptable_system_go(self) -> None:
        selected = {"source": "system", "version": "1.22.1", "binary": "/usr/bin/go"}
        with mock.patch.object(environment.shutil, "which", return_value="/usr/bin/go"), mock.patch.object(
            environment, "probe_go", return_value=selected
        ), mock.patch.object(environment, "install_go") as install:
            self.assertEqual(environment.resolve_go(), {**selected, "reused": True})
        install.assert_not_called()

    def test_missing_or_old_system_go_installs_fallback(self) -> None:
        installed = {"source": "managed", "version": environment.MANAGED_GO_VERSION}
        with mock.patch.object(environment.shutil, "which", return_value="/old/go"), mock.patch.object(
            environment, "probe_go", side_effect=environment.TsbsEnvironmentError("old")
        ), mock.patch.object(environment, "install_go", return_value=installed) as install:
            self.assertEqual(environment.resolve_go(Path("/cache")), installed)
        install.assert_called_once_with(Path("/cache"))

    def test_verify_never_installs(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(environment.shutil, "which", return_value=None), mock.patch.object(
            environment, "platform_tag", return_value="linux_amd64"
        ):
            with self.assertRaisesRegex(environment.TsbsEnvironmentError, "run prepare"):
                environment.verify_go(Path(temp))


class InstallationTests(unittest.TestCase):
    binary_content = b"#!/bin/sh\necho 'go version go1.21.13 linux/amd64'\n"

    def archive(self, root: Path, unsafe: str | None = None) -> tuple[Path, str]:
        source = root / "source" / "go" / "bin"
        source.mkdir(parents=True)
        binary = source / "go"
        binary.write_bytes(self.binary_content)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        archive = root / "go.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(root / "source" / "go", arcname="go")
            if unsafe:
                member = tarfile.TarInfo(unsafe)
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def test_install_verifies_and_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive, checksum = self.archive(root)
            metadata = {"filename": archive.name, "sha256": checksum, "source_url": "https://example.test/go.tar.gz"}

            def copy_download(_url: str, destination: Path) -> None:
                destination.write_bytes(archive.read_bytes())

            completed = mock.Mock(returncode=0, stdout="go version go1.21.13 linux/amd64\n", stderr="")
            patches = (
                mock.patch.object(environment, "platform_tag", return_value="linux_amd64"),
                mock.patch.object(environment, "release_metadata", return_value=metadata),
                mock.patch.object(environment, "download", side_effect=copy_download),
                mock.patch.object(environment.subprocess, "run", return_value=completed),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                first = environment.install_go(root / "installations")
                second = environment.install_go(root / "installations")
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["source"], "managed")
            self.assertTrue(Path(first["binary"]).is_file())

    def test_checksum_and_unsafe_archive_fail_atomically(self) -> None:
        for unsafe, expected, message in ((None, "0" * 64, "checksum mismatch"), ("../escape", None, "unsafe path")):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive, checksum = self.archive(root, unsafe)
                metadata = {"filename": archive.name, "sha256": expected or checksum, "source_url": "https://example.test/go.tar.gz"}
                with mock.patch.object(environment, "platform_tag", return_value="linux_amd64"), mock.patch.object(
                    environment, "release_metadata", return_value=metadata
                ), mock.patch.object(environment, "download", side_effect=lambda _url, destination: destination.write_bytes(archive.read_bytes())):
                    with self.assertRaisesRegex(environment.TsbsEnvironmentError, message):
                        environment.install_go(root / "installations")
                self.assertFalse((root / "installations" / environment.MANAGED_GO_VERSION / "linux_amd64").exists())

    def test_release_metadata_selects_matching_archive(self) -> None:
        payload = [{"version": "go1.21.13", "files": [{
            "filename": "go1.21.13.linux-amd64.tar.gz", "os": "linux", "arch": "amd64",
            "kind": "archive", "sha256": "a" * 64,
        }]}]
        with mock.patch.object(environment, "download_bytes", return_value=json.dumps(payload).encode()):
            selected = environment.release_metadata("linux_amd64")
        self.assertEqual(selected["sha256"], "a" * 64)


class CliTests(unittest.TestCase):
    def test_result_file_is_machine_readable(self) -> None:
        result = {
            "source": "system", "version": "1.22.0", "platform": "linux_amd64",
            "binary": "/usr/bin/go", "binary_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(setup, "resolve_go", return_value=result):
            path = Path(temp) / "result.json"
            self.assertEqual(setup.main(["prepare", "--result-file", str(path)]), 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["source"], "system")


if __name__ == "__main__":
    unittest.main()
