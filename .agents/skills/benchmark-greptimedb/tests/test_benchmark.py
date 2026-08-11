from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import benchmark  # noqa: E402
import summarize  # noqa: E402


class SummaryParserTests(unittest.TestCase):
    def test_parse_load_log_with_metrics_and_rows(self) -> None:
        parsed = summarize.parse_load_log(
            """
Summary:
loaded 200 metrics in 2.000sec with 2 workers (mean rate 100.00 metrics/sec)
loaded 40 rows in 2.000sec with 2 workers (mean rate 20.00 rows/sec)
"""
        )
        self.assertEqual(parsed["metrics"], 200)
        self.assertEqual(parsed["rows"], 40)
        self.assertEqual(parsed["metrics_per_second"], 100.0)
        self.assertEqual(parsed["rows_per_second"], 20.0)

    def test_parse_query_uses_final_all_queries_block(self) -> None:
        parsed = summarize.parse_query_log(
            """
After 100 queries with 1 workers:
all queries:
min: 1.00ms, med: 2.00ms, mean: 999.00ms, max: 4.00ms, stddev: 1.00ms, sum: 0.1sec, count: 100

Run complete after 10 queries with 1 workers (Overall query rate 3.00 queries/sec):
all queries       :
min: 1.00ms, med: 2.00ms, mean: 3.50ms, max: 4.00ms, stddev: 1.00ms, sum: 0.1sec, count: 10
wall clock time: 1.0sec
"""
        )
        self.assertEqual(parsed, {"mean_milliseconds": 3.5, "count": 10})

    def test_parse_query_rejects_incomplete_log(self) -> None:
        with self.assertRaises(summarize.SummaryError):
            summarize.parse_query_log("all queries: mean: 2.00ms")

    def test_weighted_query_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "logs").mkdir()
            (run_dir / "logs/q1.log").write_text(
                "Run complete after 2 queries\nall queries:\n"
                "min: 1ms, med: 1ms, mean: 2.00ms, max: 3ms, stddev: 1ms, sum: 0.1sec, count: 2\n"
            )
            (run_dir / "logs/q2.log").write_text(
                "Run complete after 6 queries\nall queries:\n"
                "min: 1ms, med: 1ms, mean: 4.00ms, max: 5ms, stddev: 1ms, sum: 0.1sec, count: 6\n"
            )
            manifest = {
                "run_id": "test",
                "profile": "smoke",
                "database": "benchmark",
                "events": {
                    "loads": [],
                    "queries": [
                        {
                            "query_type": "cpu-max-all-1",
                            "attempt": 1,
                            "database": "benchmark",
                            "log": "logs/q1.log",
                            "status": "completed",
                        },
                        {
                            "query_type": "cpu-max-all-1",
                            "attempt": 2,
                            "database": "benchmark",
                            "log": "logs/q2.log",
                            "status": "completed",
                        },
                    ],
                },
            }
            summary = summarize.build_summary(run_dir, manifest)
            query = summary["queries"][0]
            self.assertEqual(query["repetitions"], 2)
            self.assertEqual(query["query_count"], 8)
            self.assertEqual(query["weighted_mean_milliseconds"], 3.5)

    def test_summary_includes_dataset_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            manifest = {
                "run_id": "test",
                "profile": "smoke",
                "database": "benchmark",
                "dataset": {
                    "dataset_id": "cpu-only-s10-abc",
                    "format": "influx",
                    "data_path": "/cache/data",
                    "sha256": "1234",
                },
                "events": {"loads": [], "queries": []},
            }
            summary = summarize.build_summary(run_dir, manifest)
            self.assertEqual(summary["dataset"]["dataset_id"], "cpu-only-s10-abc")
            markdown = summarize.render_markdown(summary)
            self.assertIn("cpu-only-s10-abc", markdown)
            self.assertIn("1234", markdown)


class RunnerSafetyTests(unittest.TestCase):
    def test_database_modes(self) -> None:
        self.assertEqual(
            benchmark.database_mode_args("create", "bench", None),
            ["--do-create-db=true", "--do-abort-on-exist=true"],
        )
        self.assertEqual(
            benchmark.database_mode_args("reuse", "bench", None),
            ["--do-create-db=false"],
        )
        self.assertEqual(
            benchmark.database_mode_args("reset", "bench", "bench"),
            ["--do-create-db=true"],
        )
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.database_mode_args("reset", "bench", "other")

    def test_external_load_requires_mode_before_work(self) -> None:
        args = benchmark.make_parser().parse_args(["load", "--endpoint", "http://localhost:4000"])
        with self.assertRaisesRegex(benchmark.BenchmarkError, "database-mode"):
            benchmark.validate_args(args)

    def test_reset_confirmation_is_validated_before_work(self) -> None:
        args = benchmark.make_parser().parse_args(
            [
                "load",
                "--endpoint",
                "http://localhost:4000",
                "--database",
                "bench",
                "--database-mode",
                "reset",
                "--confirm-reset",
                "other",
            ]
        )
        with self.assertRaisesRegex(benchmark.BenchmarkError, "exactly match"):
            benchmark.validate_args(args)

    def test_attempts_are_append_only_per_query_type(self) -> None:
        events = [
            {"query_type": "a", "attempt": 1},
            {"query_type": "a", "attempt": 2},
            {"query_type": "b", "attempt": 1},
        ]
        self.assertEqual(benchmark.next_attempt(events, "a"), 3)
        self.assertEqual(benchmark.next_attempt(events, "b"), 2)

    def test_manifest_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            benchmark.save_manifest(run_dir, {"run_id": "test"})
            self.assertEqual(json.loads((run_dir / "manifest.json").read_text())["run_id"], "test")

    def test_pinned_dataset_rejects_conflicting_selection(self) -> None:
        args = benchmark.make_parser().parse_args(
            ["generate", "--only", "data", "--dataset-id", "different"]
        )
        manifest = {
            "dataset": {
                "dataset_id": "pinned",
                "dataset_path": "/tmp/pinned",
            }
        }
        with self.assertRaisesRegex(benchmark.BenchmarkError, "conflicts"):
            benchmark.dataset_selection_args(args, manifest)

    def test_pinned_dataset_path_is_reused(self) -> None:
        args = benchmark.make_parser().parse_args(["generate", "--only", "data"])
        manifest = {
            "dataset": {
                "dataset_id": "pinned",
                "dataset_path": "/tmp/pinned",
            }
        }
        self.assertEqual(
            benchmark.dataset_selection_args(args, manifest),
            ["--dataset-path", str(Path("/tmp/pinned").resolve())],
        )

    def test_changed_pinned_dataset_checksum_is_rejected(self) -> None:
        manifest = {"dataset": {"sha256": "old"}}
        dataset = {"sha256": "new", "spec": {"use_case": "cpu-only"}}
        with self.assertRaisesRegex(benchmark.BenchmarkError, "checksum changed"):
            benchmark.validate_dataset_result(manifest, dataset, False)
        benchmark.validate_dataset_result(manifest, dataset, True)

    def test_legacy_run_local_data_is_still_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "data").mkdir()
            legacy = run_dir / "data" / "influx-data.lp"
            legacy.write_text("cpu value=1 1\n", encoding="utf-8")
            manifest = {
                "run_id": "legacy",
                "workload": {},
                "events": {"loads": [], "queries": []},
            }
            args = benchmark.make_parser().parse_args(["generate", "--only", "data"])
            self.assertEqual(benchmark.generate_data(args, run_dir, manifest), legacy)
            saved = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["generated_data"], "data/influx-data.lp")

    def test_build_marker_does_not_trust_an_untracked_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "results").mkdir()
            target = run_dir / "tsbs_generate_data"
            target.touch()
            marker = run_dir / "results" / "built-tsbs_generate_data"
            self.assertTrue(benchmark.binary_needs_build(run_dir, "tsbs_generate_data", target, False))
            marker.touch()
            self.assertFalse(benchmark.binary_needs_build(run_dir, "tsbs_generate_data", target, False))
            self.assertTrue(benchmark.binary_needs_build(run_dir, "tsbs_generate_data", target, True))

    def test_generated_output_is_replaced_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "generated.dat"
            log = root / "generate.log"
            output.write_text("old\n")
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.run_tee(
                    [sys.executable, "-c", "print('partial'); raise SystemExit(2)"],
                    log,
                    stdout_path=output,
                )
            self.assertEqual(output.read_text(), "old\n")
            self.assertFalse((root / "generated.dat.tmp").exists())
            benchmark.run_tee([sys.executable, "-c", "print('new')"], log, stdout_path=output)
            self.assertEqual(output.read_text(), "new\n")

    def test_queries_are_reused_only_for_matching_generation_spec(self) -> None:
        query_type = "cpu-max-all-1"
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "queries").mkdir()
            (run_dir / "logs").mkdir()
            output = benchmark.query_path(run_dir, query_type)
            output.write_text("existing\n", encoding="utf-8")
            workload = json.loads(json.dumps(benchmark.PROFILES["smoke"]))
            manifest = {
                "workload": workload,
                "query_generation_specs": {
                    query_type: benchmark.query_generation_spec(workload, query_type),
                },
            }
            with (
                mock.patch.object(benchmark, "ensure_binaries"),
                mock.patch.object(benchmark, "run_tee") as run_tee,
            ):
                benchmark.generate_queries(run_dir, manifest, [query_type], False, False)
            run_tee.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "existing\n")

    def test_workload_changes_regenerate_queries_and_update_spec(self) -> None:
        query_type = "cpu-max-all-1"
        changes = {
            "seed": 456,
            "scale": 20,
            "start": "2023-06-10T00:00:00Z",
            "end": "2023-06-13T00:00:00Z",
            "query_count": 11,
        }
        for field, value in changes.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp:
                run_dir = Path(temp)
                (run_dir / "queries").mkdir()
                (run_dir / "logs").mkdir()
                output = benchmark.query_path(run_dir, query_type)
                output.write_text("stale\n", encoding="utf-8")
                workload = json.loads(json.dumps(benchmark.PROFILES["smoke"]))
                old_spec = benchmark.query_generation_spec(workload, query_type)
                if field == "query_count":
                    workload["query_counts"][query_type] = value
                else:
                    workload[field] = value
                manifest = {
                    "workload": workload,
                    "query_generation_specs": {query_type: old_spec},
                }

                def write_generated(_command, _log_path, *, stdout_path, **_kwargs):
                    stdout_path.write_text("fresh\n", encoding="utf-8")

                with (
                    mock.patch.object(benchmark, "ensure_binaries"),
                    mock.patch.object(benchmark, "run_tee", side_effect=write_generated) as run_tee,
                ):
                    benchmark.generate_queries(run_dir, manifest, [query_type], False, False)
                run_tee.assert_called_once()
                self.assertEqual(output.read_text(encoding="utf-8"), "fresh\n")
                expected = benchmark.query_generation_spec(workload, query_type)
                self.assertEqual(manifest["query_generation_specs"][query_type], expected)
                saved = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["query_generation_specs"][query_type], expected)

    def test_legacy_query_without_generation_spec_is_regenerated(self) -> None:
        query_type = "cpu-max-all-1"
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "queries").mkdir()
            (run_dir / "logs").mkdir()
            output = benchmark.query_path(run_dir, query_type)
            output.write_text("legacy\n", encoding="utf-8")
            workload = json.loads(json.dumps(benchmark.PROFILES["smoke"]))
            manifest = {"workload": workload}

            def write_generated(_command, _log_path, *, stdout_path, **_kwargs):
                stdout_path.write_text("fresh\n", encoding="utf-8")

            with (
                mock.patch.object(benchmark, "ensure_binaries"),
                mock.patch.object(benchmark, "run_tee", side_effect=write_generated) as run_tee,
            ):
                benchmark.generate_queries(run_dir, manifest, [query_type], False, False)
            run_tee.assert_called_once()
            self.assertEqual(output.read_text(encoding="utf-8"), "fresh\n")
            self.assertEqual(
                manifest["query_generation_specs"][query_type],
                benchmark.query_generation_spec(workload, query_type),
            )

    def test_failed_query_regeneration_preserves_previous_spec(self) -> None:
        query_type = "cpu-max-all-1"
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "queries").mkdir()
            (run_dir / "logs").mkdir()
            output = benchmark.query_path(run_dir, query_type)
            output.write_text("existing\n", encoding="utf-8")
            workload = json.loads(json.dumps(benchmark.PROFILES["smoke"]))
            previous_spec = benchmark.query_generation_spec(workload, query_type)
            workload["scale"] = 20
            manifest = {
                "workload": workload,
                "query_generation_specs": {query_type: previous_spec},
            }
            with (
                mock.patch.object(benchmark, "ensure_binaries"),
                mock.patch.object(
                    benchmark,
                    "run_tee",
                    side_effect=benchmark.BenchmarkError("generation failed"),
                ),
            ):
                with self.assertRaises(benchmark.BenchmarkError):
                    benchmark.generate_queries(run_dir, manifest, [query_type], False, False)
            self.assertEqual(output.read_text(encoding="utf-8"), "existing\n")
            self.assertEqual(manifest["query_generation_specs"][query_type], previous_spec)


if __name__ == "__main__":
    unittest.main()
