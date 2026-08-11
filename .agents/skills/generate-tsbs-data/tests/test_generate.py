from __future__ import annotations

import json
import stat
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
            _, created = generate.prepare_dataset(first)
            second = parser.parse_args(
                ["generate", "--format", "influx", "--dataset-root", temp, "--dataset-id", "shared"]
            )
            _, reused = generate.prepare_dataset(second)
            self.assertEqual(reused["spec"], created["spec"])
            self.assertEqual(reused["spec"]["scale"], 10)


class DatasetVariantTests(unittest.TestCase):
    def make_generator(self, root: Path, body: str) -> Path:
        script = root / "fake-generator"
        script.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def prepare(self, root: Path) -> tuple[Path, dict]:
        args = generate.make_parser().parse_args(
            ["generate", "--profile", "smoke", "--format", "influx", "--dataset-root", str(root)]
        )
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

    def test_corrupt_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = self.make_generator(root, "print('valid')\n")
            dataset_dir, manifest = self.prepare(root)
            with mock.patch.object(generate, "GENERATOR", fake):
                result = generate.generate_variant(dataset_dir, manifest, "influx", regenerate=False, rebuild=False)
            Path(result["data_path"]).write_text("corrupt\n", encoding="utf-8")
            with self.assertRaisesRegex(generate.DatasetError, "checksum mismatch"):
                generate.validate_variant(dataset_dir, "influx")

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
            self.assertEqual(verified["variants"][0]["sha256"], generate.sha256_file(artifact))

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


if __name__ == "__main__":
    unittest.main()
