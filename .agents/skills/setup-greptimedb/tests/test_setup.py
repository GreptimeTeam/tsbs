from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import setup  # noqa: E402


class PlatformTests(unittest.TestCase):
    def test_supported_platforms_and_artifact_names(self) -> None:
        self.assertEqual(setup.platform_tag("Linux", "x86_64"), "linux_amd64")
        self.assertEqual(setup.platform_tag("Linux", "aarch64"), "linux_arm64")
        self.assertEqual(setup.platform_tag("Darwin", "x86_64"), "darwin_amd64")
        self.assertEqual(setup.platform_tag("Darwin", "arm64"), "darwin_arm64")
        self.assertEqual(setup.artifact_name("1.1.4", "linux_amd64"), "greptime-linux-amd64-v1.1.4.tar.gz")
        with self.assertRaises(setup.SetupError):
            setup.platform_tag("Windows", "AMD64")


class VersionResolutionTests(unittest.TestCase):
    def test_normalizes_explicit_versions_and_latest_stable(self) -> None:
        self.assertEqual(setup.normalize_version("v1.1.4"), "1.1.4")
        self.assertEqual(setup.normalize_version("1.2.0-beta.1"), "1.2.0-beta.1")
        payload = json.dumps({"tag_name": "v1.1.4", "draft": False, "prerelease": False}).encode()
        with mock.patch.object(setup, "download_bytes", return_value=payload):
            self.assertEqual(setup.resolve_official_version(), "1.1.4")
        for release in ({"tag_name": "v1.2.0-beta.1", "prerelease": True}, {"tag_name": "bad"}):
            with self.subTest(release=release), mock.patch.object(setup, "download_bytes", return_value=json.dumps(release).encode()):
                with self.assertRaisesRegex(setup.SetupError, "exact --version"):
                    setup.resolve_official_version()

    def test_request_uses_optional_github_token(self) -> None:
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "secret"}):
            value = setup.request("https://api.github.test", accept="application/vnd.github+json")
        self.assertEqual(value.get_header("User-agent"), setup.USER_AGENT)
        self.assertEqual(value.get_header("Authorization"), "Bearer secret")

    def test_download_streams_response_to_destination(self) -> None:
        payload = b"release archive contents"

        class StreamingResponse(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    raise AssertionError("download must not buffer the complete response")
                return super().read(size)

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifact.tar.gz"
            with mock.patch.object(setup.urllib.request, "urlopen", return_value=StreamingResponse(payload)):
                setup.download("https://example.test/artifact.tar.gz", destination)
            self.assertEqual(destination.read_bytes(), payload)


class InstallationTests(unittest.TestCase):
    binary_content = b"#!/bin/sh\necho 'greptime 1.1.4'\n"

    def archive(self, root: Path, *, unsafe: str | None = None) -> str:
        distribution = root / "greptime-linux-amd64-v1.1.4"
        distribution.mkdir()
        binary = distribution / "greptime"; binary.write_bytes(self.binary_content); binary.chmod(0o755)
        (distribution / "README.txt").write_text("release", encoding="utf-8")
        archive = root / "artifact.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(distribution, arcname=distribution.name, recursive=True)
            if unsafe:
                member = tarfile.TarInfo(unsafe); member.size = 1; bundle.addfile(member, io.BytesIO(b"x"))
        return hashlib.sha256(archive.read_bytes()).hexdigest()

    def args(self, root: Path, reinstall: bool = False) -> argparse.Namespace:
        return argparse.Namespace(version="1.1.4", version_source="explicit", install_root=root, reinstall=reinstall)

    def test_install_verifies_reuses_and_detects_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source"; source.mkdir(); checksum = self.archive(source)
            def download(_url: str, destination: Path) -> None:
                if destination.name.endswith(".sha256sum"):
                    destination.write_text(checksum + "  artifact.tar.gz\n", encoding="utf-8")
                else:
                    destination.write_bytes((source / "artifact.tar.gz").read_bytes())
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), mock.patch.object(setup, "download", side_effect=download):
                first = setup.install(self.args(root / "installations")); second = setup.install(self.args(root / "installations"))
            self.assertFalse(first["reused"]); self.assertTrue(second["reused"])
            installation = Path(first["installation_path"]); (installation / "README.txt").write_text("corrupt", encoding="utf-8")
            with self.assertRaisesRegex(setup.SetupError, "distribution checksum mismatch"):
                setup.validate_installation(installation)

    def test_checksum_and_unsafe_archive_fail_atomically(self) -> None:
        for unsafe, expected_checksum, message in ((None, "0" * 64, "checksum mismatch"), ("../escape", None, "top-level directory")):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source = root / "source"; source.mkdir(); checksum = self.archive(source, unsafe=unsafe)
                def download(_url: str, destination: Path) -> None:
                    if destination.name.endswith(".sha256sum"):
                        destination.write_text(expected_checksum or checksum, encoding="utf-8")
                    else:
                        destination.write_bytes((source / "artifact.tar.gz").read_bytes())
                args = self.args(root / "installations")
                with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), mock.patch.object(setup, "download", side_effect=download):
                    with self.assertRaisesRegex(setup.SetupError, message):
                        setup.install(args)
                self.assertFalse(setup.installation_path(args, "linux_amd64").exists())


class DatabaseTests(unittest.TestCase):
    def make_installation(self, root: Path) -> Path:
        path = root / "installations/1.1.4/linux_amd64"; path.mkdir(parents=True)
        binary = path / "greptime"; binary.write_text("#!/bin/sh\necho 'greptime 1.1.4'\n", encoding="utf-8"); binary.chmod(0o755)
        manifest = {
            "schema_version": 1, "kind": "greptimedb-installation", "version": "1.1.4",
            "version_source": "explicit", "platform": "linux_amd64", "binary": "greptime",
            "binary_sha256": setup.sha256_file(binary), "archive_sha256": "a" * 64,
            "distribution_sha256": setup.distribution_sha256(path),
        }
        setup.save_json(path / "manifest.json", manifest); return path

    def args(self, root: Path, version: str = "1.1.4") -> argparse.Namespace:
        return argparse.Namespace(database_id="db-a", version=version, version_source="explicit", database="benchmark", install_root=root / "installations", database_root=root / "databases")

    def test_prepare_reuse_and_immutable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.make_installation(root); args = self.args(root)
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"):
                value = setup.prepare(args); reused = setup.prepare(args)
            self.assertFalse(value["reused"]); self.assertTrue(reused["reused"])
            self.assertEqual(value["database"], "benchmark")
            manifest = setup.validate_database(Path(value["database_path"]), "db-a")
            self.assertEqual(manifest["version"], "1.1.4")
            args.database = "other"
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), self.assertRaisesRegex(setup.SetupError, "already bound"):
                setup.prepare(args)

    def test_legacy_workspace_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.make_installation(root); path = root / "databases/db-a"; path.mkdir(parents=True)
            setup.save_json(path / "manifest.json", {"schema_version": 1, "kind": "greptimedb-database", "database_id": "db-a", "database": "benchmark", "binding": None})
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), self.assertRaisesRegex(setup.SetupError, "legacy"):
                setup.prepare(self.args(root))


if __name__ == "__main__":
    unittest.main()
