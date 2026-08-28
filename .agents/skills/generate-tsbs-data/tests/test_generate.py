from __future__ import annotations

import io
import gzip
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import generate  # noqa: E402


class DatasetIdentityTests(unittest.TestCase):
    def test_id_is_stable_and_format_independent(self) -> None:
        spec = dict(generate.PROFILES["smoke"])
        reordered = dict(reversed(list(spec.items())))
        self.assertEqual(generate.automatic_dataset_id(spec), generate.automatic_dataset_id(reordered))
        changed = dict(spec, scale=11)
        self.assertNotEqual(generate.automatic_dataset_id(spec), generate.automatic_dataset_id(changed))
        self.assertEqual(generate.automatic_dataset_id(spec), generate.automatic_dataset_id(spec, "none"))
        self.assertNotEqual(generate.automatic_dataset_id(spec), generate.automatic_dataset_id(spec, "gzip"))

    def test_dataset_selection_flags_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            generate.make_parser().parse_args(
                ["generate", "--format", "influx", "--dataset-id", "one", "--dataset-path", "/tmp/two"]
            )

    def test_existing_named_dataset_rejects_different_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parser = generate.make_parser()
            first = parser.parse_args(
                ["generate", "--format", "influx", "--dataset-root", temp, "--dataset-id", "shared", "--scale", "10"]
            )
            generate.prepare_dataset(first)
            second = parser.parse_args(
                ["generate", "--format", "influx", "--dataset-root", temp, "--dataset-id", "shared", "--scale", "11"]
            )
            with self.assertRaisesRegex(generate.DatasetError, "do not match"):
                generate.prepare_dataset(second)

    def test_existing_named_dataset_inherits_stored_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parser = generate.make_parser()
            first = parser.parse_args(
                ["generate", "--format", "influx", "--dataset-root", temp, "--dataset-id", "shared", "--profile", "smoke"]
            )
            dataset_dir, created = generate.prepare_dataset(first)
            legacy = dict(created)
            legacy.pop("compression")
            generate.save_json(dataset_dir / "dataset.json", legacy)
            second = parser.parse_args(
                ["generate", "--format", "influx", "--dataset-root", temp, "--dataset-id", "shared"]
            )
            _, reused = generate.prepare_dataset(second)
            self.assertEqual(reused["spec"], created["spec"])
            self.assertEqual(reused["spec"]["scale"], 10)
            self.assertEqual(generate.manifest_compression(reused), "none")

    def test_existing_dataset_inherits_compression_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            parser = generate.make_parser()
            first = parser.parse_args([
                "prepare", "--dataset-root", temp, "--dataset-id", "shared",
                "--profile", "smoke", "--compression", "gzip",
            ])
            generate.prepare_dataset(first)
            inherited = parser.parse_args([
                "generate", "--dataset-root", temp, "--dataset-id", "shared",
                "--format", "influx",
            ])
            _, inherited_manifest = generate.prepare_dataset(inherited)
            self.assertEqual(generate.manifest_compression(inherited_manifest), "gzip")
            conflicting = parser.parse_args([
                "generate", "--dataset-root", temp, "--dataset-id", "shared",
                "--format", "influx", "--compression", "none",
            ])
            with self.assertRaisesRegex(generate.DatasetError, "conflicts"):
                generate.prepare_dataset(conflicting)

    def test_prepare_creates_metadata_only_logical_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result_file = Path(temp) / "result.json"
            code = generate.main(
                ["prepare", "--profile", "smoke", "--dataset-root", temp,
                 "--result-file", str(result_file)]
            )
            self.assertEqual(code, 0)
            result = json.loads(result_file.read_text(encoding="utf-8"))
            dataset = Path(result["dataset_path"])
            self.assertTrue((dataset / "dataset.json").is_file())
            self.assertFalse((dataset / "formats").exists())
            self.assertNotIn("data_path", result)
            second = generate.make_parser().parse_args(
                ["prepare", "--profile", "smoke", "--dataset-root", temp]
            )
            selected = generate.resolve_dataset_path(second, generate.logical_spec(second))
            self.assertTrue((selected / "dataset.json").is_file())

class DatasetVariantTests(unittest.TestCase):
    def make_generator(self, root: Path, body: str) -> Path:
        script = root / "fake-generator"
        script.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def prepare(self, root: Path, compression: str = "none") -> tuple[Path, dict]:
        command = ["generate", "--profile", "smoke", "--format", "influx", "--dataset-root", str(root)]
        if compression != "none":
            command.extend(["--compression", compression])
        args = generate.make_parser().parse_args(command)
        return generate.prepare_dataset(args)

    def test_multiple_formats_share_logical_dataset_and_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(
                root,
                "import sys\n"
                "print('payload:' + next(a for a in sys.argv if a.startswith('--format=')))\n",
            )
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", fake):
                influx = generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
                timescale = generate.generate_variant(
                    dataset_dir, manifest, "timescaledb", regenerate=False, rebuild=False
                )
                reused = generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
            self.assertEqual(influx["dataset_id"], timescale["dataset_id"])
            self.assertFalse(influx["reused"])
            self.assertTrue(reused["reused"])
            self.assertNotEqual(influx["sha256"], timescale["sha256"])

    def test_gzip_output_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid payload')\n")
            dataset_dir, manifest = self.prepare(root, "gzip")
            with mock.patch.object(generate, "GENERATOR", fake):
                first = generate.generate_variant(dataset_dir, manifest, "influx", compression="gzip", regenerate=False, rebuild=False)
                second = generate.generate_variant(dataset_dir, manifest, "influx", compression="gzip", regenerate=True, rebuild=False)
            self.assertEqual(first["sha256"], second["sha256"])
            artifact = Path(second["data_path"])
            self.assertEqual(second["bytes"], artifact.stat().st_size)
            self.assertEqual(second["sha256"], generate.stored_file_sha256(artifact))
            with gzip.open(artifact, "rb") as stream:
                self.assertEqual(stream.read(), b"valid payload\n")
            variant = json.loads((artifact.parent / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(variant["schema_version"], 1)
            self.assertNotIn("artifact_bytes", variant)
            self.assertNotIn("artifact_sha256", variant)

    def test_reuse_skips_checksum_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid')\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", fake):
                generate.generate_variant(
                    dataset_dir, manifest, "influx", regenerate=False, rebuild=False
                )
                with mock.patch.object(
                    generate,
                    "stored_file_sha256",
                    side_effect=AssertionError("reuse must not hash the artifact"),
                ):
                    reused = generate.generate_variant(
                        dataset_dir,
                        manifest,
                        "influx",
                        regenerate=False,
                        rebuild=False,
                    )
            self.assertTrue(reused["reused"])

    def test_reuse_rejects_artifact_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid')\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", fake):
                result = generate.generate_variant(
                    dataset_dir, manifest, "influx", regenerate=False, rebuild=False
                )
                Path(result["data_path"]).write_text("longer payload\n", encoding="utf-8")
                with self.assertRaisesRegex(generate.DatasetError, "size mismatch"):
                    generate.generate_variant(
                        dataset_dir,
                        manifest,
                        "influx",
                        regenerate=False,
                        rebuild=False,
                    )

    def test_explicit_verify_rejects_same_size_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid')\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", fake):
                result = generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
            Path(result["data_path"]).write_text("bad!!\n", encoding="utf-8")
            verify_args = generate.make_parser().parse_args(
                ["verify", "--dataset-path", str(dataset_dir), "--format", "influx"]
            )
            with self.assertRaisesRegex(generate.DatasetError, "checksum mismatch"):
                generate.verify_dataset(verify_args)

    def test_schema_v2_is_rejected_and_can_be_regenerated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid')\n")
            dataset_dir, manifest = self.prepare(root, "gzip")
            with mock.patch.object(generate, "GENERATOR", fake):
                result = generate.generate_variant(dataset_dir, manifest, "influx", compression="gzip", regenerate=False, rebuild=False)
            artifact = Path(result["data_path"])
            manifest_path = artifact.parent / "manifest.json"
            variant = json.loads(manifest_path.read_text(encoding="utf-8"))
            variant["schema_version"] = 2
            variant["artifact_bytes"] = artifact.stat().st_size
            variant["artifact_sha256"] = generate.stored_file_sha256(artifact)
            manifest_path.write_text(json.dumps(variant), encoding="utf-8")
            with self.assertRaisesRegex(generate.DatasetError, "--regenerate"):
                generate.generate_variant(
                    dataset_dir, manifest, "influx", compression="gzip", regenerate=False, rebuild=False
                )
            list_args = generate.make_parser().parse_args(["list", "--dataset-root", str(root)])
            self.assertEqual(generate.list_datasets(list_args)[0]["formats"], [])
            with mock.patch.object(generate, "GENERATOR", fake):
                regenerated = generate.generate_variant(
                    dataset_dir, manifest, "influx", compression="gzip", regenerate=True, rebuild=False
                )
            self.assertFalse(regenerated["reused"])
            replaced = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(replaced["schema_version"], 1)
            self.assertNotIn("artifact_sha256", replaced)

    def test_failed_regeneration_preserves_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = self.make_generator(root, "print('original')\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", good):
                original = generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
            data_path = Path(original["data_path"])
            manifest_path = data_path.parent / "manifest.json"
            old_data = data_path.read_bytes()
            old_manifest = manifest_path.read_bytes()
            bad = self.make_generator(root, "import sys\nprint('partial')\nraise SystemExit(2)\n")
            with mock.patch.object(generate, "GENERATOR", bad):
                with self.assertRaises(generate.DatasetError):
                    generate.generate_variant(dataset_dir, manifest, "influx", regenerate=True, rebuild=False)
            self.assertEqual(data_path.read_bytes(), old_data)
            self.assertEqual(manifest_path.read_bytes(), old_manifest)

    def test_failed_gzip_and_checksum_preserve_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generator = self.make_generator(root, "print('original')\n")
            dataset_dir, manifest = self.prepare(root, "gzip")
            with mock.patch.object(generate, "GENERATOR", generator):
                original = generate.generate_variant(
                    dataset_dir, manifest, "influx", compression="gzip", regenerate=False, rebuild=False
                )
            data_path = Path(original["data_path"])
            manifest_path = data_path.parent / "manifest.json"
            old_data = data_path.read_bytes()
            old_manifest = manifest_path.read_bytes()

            bad_gzip_root = root / "bad-gzip"
            bad_gzip_root.mkdir()
            bad_gzip = self.make_generator(
                bad_gzip_root,
                "import sys\nsys.stdin.buffer.read()\nraise SystemExit(2)\n",
            )
            with mock.patch.object(generate, "GENERATOR", generator), mock.patch.object(
                generate, "gzip_command", return_value=[str(bad_gzip)]
            ):
                with self.assertRaisesRegex(generate.DatasetError, "gzip failed"):
                    generate.generate_variant(
                        dataset_dir, manifest, "influx", compression="gzip", regenerate=True, rebuild=False
                    )
            self.assertEqual(data_path.read_bytes(), old_data)
            self.assertEqual(manifest_path.read_bytes(), old_manifest)

            with mock.patch.object(generate, "GENERATOR", generator), mock.patch.object(
                generate, "stored_file_sha256", side_effect=generate.DatasetError("checksum failed")
            ):
                with self.assertRaisesRegex(generate.DatasetError, "checksum failed"):
                    generate.generate_variant(
                        dataset_dir, manifest, "influx", compression="gzip", regenerate=True, rebuild=False
                    )
            self.assertEqual(data_path.read_bytes(), old_data)
            self.assertEqual(manifest_path.read_bytes(), old_manifest)

    def test_failed_initial_generation_can_be_retried_without_regenerate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = self.make_generator(root, "import sys\nprint('partial')\nraise SystemExit(2)\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", bad):
                with self.assertRaises(generate.DatasetError):
                    generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
            manifest_path = dataset_dir / "formats" / "influx" / "manifest.json"
            failed = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "failed")

            good = self.make_generator(root, "print('complete')\n")
            with mock.patch.object(generate, "GENERATOR", good):
                retried = generate.generate_variant(
                    dataset_dir, manifest, "influx", regenerate=False, rebuild=False
                )
            self.assertFalse(retried["reused"])
            self.assertTrue(Path(retried["data_path"]).is_file())
            completed = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "completed")

    def test_list_and_verify_report_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid')\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", fake):
                generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
            list_args = generate.make_parser().parse_args(["list", "--dataset-root", str(root)])
            listed = generate.list_datasets(list_args)
            self.assertEqual(listed[0]["formats"], ["influx"])
            verify_args = generate.make_parser().parse_args(
                ["verify", "--dataset-path", str(dataset_dir), "--format", "influx"]
            )
            verified = generate.verify_dataset(verify_args)
            artifact = Path(verified["variants"][0]["data_path"])
            self.assertEqual(verified["variants"][0]["sha256"], generate.stored_file_sha256(artifact))

    def test_checksum_command_prefers_sha256sum_and_falls_back_to_shasum(self) -> None:
        with mock.patch.object(generate.shutil, "which", side_effect=lambda name: "/usr/bin/sha256sum" if name == "sha256sum" else None):
            self.assertEqual(generate.sha256_command(), ["/usr/bin/sha256sum"])
        with mock.patch.object(generate.shutil, "which", side_effect=lambda name: "/usr/bin/shasum" if name == "shasum" else None):
            self.assertEqual(generate.sha256_command(), ["/usr/bin/shasum", "-a", "256"])

    def test_checksum_command_reports_missing_tools_and_bad_output(self) -> None:
        with mock.patch.object(generate.shutil, "which", return_value=None):
            with self.assertRaisesRegex(generate.DatasetError, "sha256sum or shasum"):
                generate.sha256_command()
        completed = subprocess.CompletedProcess(["sha256sum"], 0, stdout="not-a-checksum  file\n", stderr="")
        with mock.patch.object(generate, "sha256_command", return_value=["sha256sum"]), mock.patch.object(
            generate.subprocess, "run", return_value=completed
        ):
            with self.assertRaisesRegex(generate.DatasetError, "invalid SHA-256 output"):
                generate.stored_file_sha256(Path("file"))

    def test_result_file_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result_file = root / "result.json"
            fake = self.make_generator(root, "print('valid')\n")
            with mock.patch.object(generate, "GENERATOR", fake):
                code = generate.main(
                    [
                        "generate",
                        "--profile",
                        "smoke",
                        "--format",
                        "influx",
                        "--dataset-root",
                        str(root / "datasets"),
                        "--result-file",
                        str(result_file),
                    ]
                )
            self.assertEqual(code, 0)
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(result["format"], "influx")
            self.assertTrue(Path(result["data_path"]).is_file())

    def test_build_uses_resolved_absolute_go_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generator = root / "bin" / "tsbs_generate_data"
            build_metadata = root / "bin" / "tsbs_generate_data.build.json"
            toolchain = {"source": "managed", "version": "1.21.13", "binary": "/managed/go", "binary_sha256": "a" * 64}

            class Process:
                stdout = io.StringIO("")

                def wait(self):
                    generator.parent.mkdir(parents=True, exist_ok=True)
                    generator.write_text("binary", encoding="utf-8")
                    return 0

            with mock.patch.object(generate, "REPO_ROOT", root), mock.patch.object(generate, "GENERATOR", generator), mock.patch.object(
                generate, "GENERATOR_BUILD_METADATA", build_metadata
            ), mock.patch.object(
                generate, "resolve_go", return_value=toolchain
            ), mock.patch.object(generate.subprocess, "Popen", return_value=Process()) as popen:
                selected = generate.run_build(root / "build.log", False)
            self.assertEqual(selected, toolchain)
            self.assertEqual(popen.call_args.args[0][0], "/managed/go")
            self.assertEqual(json.loads(build_metadata.read_text(encoding="utf-8"))["go_toolchain"], toolchain)
            self.assertIn('"source": "managed"', (root / "build.log").read_text(encoding="utf-8"))

            with mock.patch.object(generate, "GENERATOR", generator), mock.patch.object(generate, "GENERATOR_BUILD_METADATA", build_metadata), mock.patch.object(
                generate, "resolve_go"
            ) as resolver:
                reused = generate.run_build(root / "build.log", False)
            self.assertEqual(reused, toolchain)
            resolver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
