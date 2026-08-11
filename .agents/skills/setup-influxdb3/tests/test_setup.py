from __future__ import annotations

import argparse
import hashlib
import io
import json
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
        self.assertEqual(setup.platform_tag("Darwin", "arm64"), "darwin_arm64")
        self.assertEqual(
            setup.artifact_name("enterprise", "3.11.1", "linux_amd64"),
            "influxdb3-enterprise-3.11.1_linux_amd64.tar.gz",
        )
        with self.assertRaises(setup.SetupError):
            setup.platform_tag("Darwin", "x86_64")


class InstallationTests(unittest.TestCase):
    def archive(self, path: Path, content: bytes = b"binary") -> str:
        payload = path / "payload"
        payload.write_bytes(content)
        with tarfile.open(path / "artifact.tar.gz", "w:gz") as bundle:
            bundle.add(payload, arcname="influxdb3")
        return hashlib.sha256((path / "artifact.tar.gz").read_bytes()).hexdigest()

    def args(self, root: Path, reinstall: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            edition="core", version="3.11.1", install_root=root, reinstall=reinstall
        )

    def test_install_verifies_and_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source"; source.mkdir()
            checksum = self.archive(source)

            def download(_url: str, destination: Path) -> None:
                if destination.name.endswith(".sha256"):
                    destination.write_text(checksum + "  archive.tar.gz\n", encoding="utf-8")
                else:
                    destination.write_bytes((source / "artifact.tar.gz").read_bytes())

            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), mock.patch.object(setup, "download", side_effect=download):
                first = setup.install(self.args(root / "installations"))
                second = setup.install(self.args(root / "installations"))
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(first["binary_sha256"], hashlib.sha256(b"binary").hexdigest())

    def test_checksum_failure_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source"; source.mkdir(); self.archive(source)
            def download(_url: str, destination: Path) -> None:
                if destination.name.endswith(".sha256"):
                    destination.write_text("0" * 64, encoding="utf-8")
                else:
                    destination.write_bytes((source / "artifact.tar.gz").read_bytes())
            args = self.args(root / "installations")
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), mock.patch.object(setup, "download", side_effect=download):
                with self.assertRaisesRegex(setup.SetupError, "checksum mismatch"):
                    setup.install(args)
            self.assertFalse(setup.installation_path(args, "linux_amd64").exists())


class InstanceTests(unittest.TestCase):
    def make_installation(self, root: Path, edition: str = "core") -> Path:
        path = root / "installations" / edition / "3.11.1" / "linux_amd64"
        path.mkdir(parents=True); binary = path / "influxdb3"; binary.write_bytes(b"binary")
        binary.chmod(0o755)
        setup.save_json(path / "manifest.json", {
            "schema_version": 1, "kind": "influxdb3-installation", "edition": edition,
            "version": "3.11.1", "platform": "linux_amd64", "binary": "influxdb3",
            "binary_sha256": setup.sha256_file(binary), "archive_sha256": "a" * 64,
        })
        return path

    def args(self, root: Path, edition: str = "core") -> argparse.Namespace:
        return argparse.Namespace(
            instance_id=f"{edition}-a", edition=edition, version="3.11.1",
            install_root=root / "installations", instance_root=root / "instances",
        )

    def test_prepare_core_and_enterprise_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for edition in ("core", "enterprise"):
                self.make_installation(root, edition)
                args = self.args(root, edition)
                with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"):
                    value = setup.prepare(args)
                    reused = setup.prepare(args)
                self.assertEqual(value["edition"], edition)
                self.assertEqual(value["cluster_id"] is not None, edition == "enterprise")
                self.assertTrue(reused["reused"])

    def test_activation_manifest_does_not_store_email(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.make_installation(root, "enterprise")
            prepare_args = self.args(root, "enterprise")
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"):
                setup.prepare(prepare_args)
            args = argparse.Namespace(
                instance_id="enterprise-a", instance_root=root / "instances",
                license_file=None, license_type="trial", license_email_env="PRIVATE_LICENSE_EMAIL",
                http_port=8181, activation_timeout=5,
            )
            process = mock.Mock(); process.poll.return_value = None; process.pid = 123
            process.wait.return_value = 0
            with mock.patch.dict("os.environ", {"PRIVATE_LICENSE_EMAIL": "private@example.com"}), mock.patch.object(setup, "port_available", return_value=True), mock.patch.object(setup.subprocess, "Popen", return_value=process), mock.patch.object(setup, "wait_health", return_value=True), mock.patch.object(setup.os, "killpg"):
                value = setup.activate(args)
            persisted = json.dumps(value)
            self.assertNotIn("private@example.com", persisted)
            self.assertEqual(value["license"], {"status": "active", "source": "trial", "path": None})


class ArgumentTests(unittest.TestCase):
    def test_exact_version_and_activation_email_are_required(self) -> None:
        args = setup.make_parser().parse_args(["install", "--edition", "core", "--version", "latest"])
        with self.assertRaisesRegex(setup.SetupError, "exact semantic version"):
            setup.validate_args(args)
        args = setup.make_parser().parse_args(["activate", "--instance-id", "ent", "--license-type", "home"])
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(setup.SetupError, "INFLUXDB3_LICENSE_EMAIL"):
                setup.validate_args(args)


if __name__ == "__main__":
    unittest.main()
