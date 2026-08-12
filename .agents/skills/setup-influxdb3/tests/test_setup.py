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
        self.assertEqual(setup.platform_tag("Darwin", "arm64"), "darwin_arm64")
        self.assertEqual(
            setup.artifact_name("enterprise", "3.11.1", "linux_amd64"),
            "influxdb3-enterprise-3.11.1_linux_amd64.tar.gz",
        )
        with self.assertRaises(setup.SetupError):
            setup.platform_tag("Darwin", "x86_64")


class InstallationTests(unittest.TestCase):
    binary_content = b"#!/bin/sh\necho 'influxdb3 InfluxDB 3 Core, 3.11.1, revision test'\n"

    def archive(self, path: Path, *, unsafe: str | None = None) -> str:
        root = path / "influxdb3-core-3.11.1"
        (root / "python/lib").mkdir(parents=True)
        binary = root / "influxdb3"
        binary.write_bytes(self.binary_content)
        binary.chmod(0o755)
        (root / "python/lib/libpython3.so").write_bytes(b"python")
        os.symlink("libpython3.so", root / "python/lib/libpython.so")
        with tarfile.open(path / "artifact.tar.gz", "w:gz") as bundle:
            bundle.add(root, arcname=root.name, recursive=True)
            if unsafe is not None:
                member = tarfile.TarInfo(unsafe)
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
        return hashlib.sha256((path / "artifact.tar.gz").read_bytes()).hexdigest()

    def args(self, root: Path, reinstall: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            edition="core", version="3.11.1", version_source="explicit",
            install_root=root, reinstall=reinstall
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
            self.assertEqual(first["schema_version"], setup.INSTALLATION_SCHEMA_VERSION)
            self.assertEqual(first["binary_sha256"], hashlib.sha256(self.binary_content).hexdigest())
            installation = Path(first["installation_path"])
            self.assertTrue((installation / "python/lib/libpython3.so").is_file())
            self.assertTrue((installation / "python/lib/libpython.so").is_symlink())

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

    def test_distribution_corruption_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source"; source.mkdir(); checksum = self.archive(source)
            def download(_url: str, destination: Path) -> None:
                if destination.name.endswith(".sha256"):
                    destination.write_text(checksum, encoding="utf-8")
                else:
                    destination.write_bytes((source / "artifact.tar.gz").read_bytes())
            args = self.args(root / "installations")
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), mock.patch.object(setup, "download", side_effect=download):
                value = setup.install(args)
            (Path(value["installation_path"]) / "python/lib/libpython3.so").write_bytes(b"corrupt")
            with self.assertRaisesRegex(setup.SetupError, "distribution checksum mismatch"):
                setup.validate_installation(Path(value["installation_path"]))

    def test_unsafe_archive_is_rejected_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source"; source.mkdir(); checksum = self.archive(source, unsafe="../escape")
            def download(_url: str, destination: Path) -> None:
                if destination.name.endswith(".sha256"):
                    destination.write_text(checksum, encoding="utf-8")
                else:
                    destination.write_bytes((source / "artifact.tar.gz").read_bytes())
            args = self.args(root / "installations")
            with mock.patch.object(setup, "platform_tag", return_value="linux_amd64"), mock.patch.object(setup, "download", side_effect=download):
                with self.assertRaisesRegex(setup.SetupError, "top-level directory"):
                    setup.install(args)
            self.assertFalse(setup.installation_path(args, "linux_amd64").exists())


class VersionResolutionTests(unittest.TestCase):
    def test_download_request_uses_explicit_identity_and_accept_header(self) -> None:
        request = setup.request("https://example.test/artifact")
        self.assertEqual(request.get_header("User-agent"), setup.USER_AGENT)
        self.assertEqual(request.get_header("Accept"), "application/octet-stream,*/*")

    def test_core_and_enterprise_resolve_from_official_installer(self) -> None:
        script = 'INFLUXDB_OSS_VERSION="3.11.1"\nINFLUXDB_ENT_VERSION="3.12.0"\n'
        with mock.patch.object(setup, "download_text", return_value=script):
            self.assertEqual(setup.resolve_official_version("core"), "3.11.1")
            self.assertEqual(setup.resolve_official_version("enterprise"), "3.12.0")

    def test_resolution_requires_one_semantic_assignment(self) -> None:
        for script in ('', 'INFLUXDB_OSS_VERSION="latest"', 'INFLUXDB_OSS_VERSION="3.11.1"\nINFLUXDB_OSS_VERSION="3.12.0"'):
            with self.subTest(script=script), mock.patch.object(setup, "download_text", return_value=script):
                with self.assertRaisesRegex(setup.SetupError, "provide an exact --version"):
                    setup.resolve_official_version("core")

    def test_explicit_version_bypasses_resolution(self) -> None:
        explicit = setup.make_parser().parse_args(["install", "--edition", "core", "--version", "3.10.5"])
        with mock.patch.object(setup, "resolve_official_version") as resolver:
            setup.resolve_args_version(explicit)
        resolver.assert_not_called()
        self.assertEqual((explicit.version, explicit.version_source), ("3.10.5", "explicit"))

        latest = setup.make_parser().parse_args(["prepare", "--edition", "core", "--instance-id", "core-a"])
        with mock.patch.object(setup, "resolve_official_version", return_value="3.11.1"):
            setup.resolve_args_version(latest)
        self.assertEqual((latest.version, latest.version_source), ("3.11.1", "official-installer"))


class InstanceTests(unittest.TestCase):
    def make_installation(self, root: Path, edition: str = "core") -> Path:
        path = root / "installations" / edition / "3.11.1" / "linux_amd64"
        path.mkdir(parents=True); binary = path / "influxdb3"
        name = "Core" if edition == "core" else "Enterprise"
        binary.write_text(f"#!/bin/sh\necho 'influxdb3 InfluxDB 3 {name}, 3.11.1, revision test'\n", encoding="utf-8")
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
            version_source="explicit",
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
                self.assertEqual(value["version_source"], "explicit")
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
                license_email_stdin=False,
                http_port=8181, activation_timeout=5,
            )
            process = mock.Mock(); process.poll.return_value = None; process.pid = 123
            process.stdout = io.StringIO("activation for private@example.com\n")
            process.wait.return_value = 0
            with mock.patch.dict("os.environ", {"PRIVATE_LICENSE_EMAIL": "private@example.com"}), mock.patch.object(setup, "verify_binary"), mock.patch.object(setup, "port_available", return_value=True), mock.patch.object(setup.subprocess, "Popen", return_value=process), mock.patch.object(setup, "wait_health", return_value=True), mock.patch.object(setup.os, "killpg"):
                value = setup.activate(args)
            persisted = json.dumps(value)
            self.assertNotIn("private@example.com", persisted)
            activation_log = root / "instances/enterprise-a/logs/license-activation.log"
            self.assertNotIn("private@example.com", activation_log.read_text())
            self.assertIn("<redacted-email>", activation_log.read_text())
            self.assertEqual(value["license"], {"status": "active", "source": "trial", "path": None})

    def test_stdin_email_takes_precedence_and_generic_emails_are_redacted(self) -> None:
        args = setup.make_parser().parse_args([
            "activate", "--instance-id", "enterprise-a", "--license-type", "home",
            "--license-email-stdin",
        ])
        stdin = mock.Mock(); stdin.isatty.return_value = False; stdin.readline.return_value = "stdin@example.com\n"
        with mock.patch.object(setup.sys, "stdin", stdin), mock.patch.dict("os.environ", {"INFLUXDB3_LICENSE_EMAIL": "env@example.com"}):
            self.assertEqual(setup.read_license_email(args), "stdin@example.com")
        self.assertEqual(
            setup.redact_emails("known stdin@example.com unknown other@example.org", ("stdin@example.com",)),
            "known <redacted-email> unknown <redacted-email>",
        )

    def test_port_probe_enables_address_reuse_and_retries(self) -> None:
        sock = mock.MagicMock()
        sock.__enter__.return_value = sock
        with mock.patch.object(setup.socket, "socket", return_value=sock):
            self.assertTrue(setup.port_available(8181))
        sock.setsockopt.assert_called_once_with(setup.socket.SOL_SOCKET, setup.socket.SO_REUSEADDR, 1)
        with mock.patch.object(setup, "port_available", side_effect=[False, True]), mock.patch.object(setup.time, "sleep"):
            self.assertTrue(setup.wait_port_available(8181))


class ArgumentTests(unittest.TestCase):
    def test_version_must_be_omitted_or_exact_and_activation_email_is_required(self) -> None:
        args = setup.make_parser().parse_args(["install", "--edition", "core", "--version", "latest"])
        with self.assertRaisesRegex(setup.SetupError, "omitted or be an exact semantic version"):
            setup.validate_args(args)
        setup.validate_args(setup.make_parser().parse_args(["install", "--edition", "core"]))
        args = setup.make_parser().parse_args(["activate", "--instance-id", "ent", "--license-type", "home"])
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(setup.SetupError, "INFLUXDB3_LICENSE_EMAIL"):
                setup.validate_args(args)
        stdin_args = setup.make_parser().parse_args(["activate", "--instance-id", "ent", "--license-type", "home", "--license-email-stdin"])
        with mock.patch.dict("os.environ", {}, clear=True):
            setup.validate_args(stdin_args)


if __name__ == "__main__":
    unittest.main()
