from __future__ import annotations

import argparse
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


class SummaryTests(unittest.TestCase):
    def write_json(self, root: Path, relative: str, value: dict) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_reused_load_without_log_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            summary = summarize.build_summary(
                Path(temp),
                {
                    "events": {
                        "loads": [
                            {
                                "attempt": 1,
                                "database": "benchmark",
                                "database_mode": "reuse",
                                "status": "reused",
                            }
                        ],
                        "queries": [],
                    }
                },
            )

        self.assertEqual(summary["ingestion_runs"], [])
        self.assertEqual(summary["failures"], [])

    def test_structured_results_do_not_require_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.write_json(
                run_dir,
                "results/load.json",
                {
                    "ResultFormatVersion": "0.2",
                    "DurationMillis": 2000,
                    "Totals": {
                        "metricCount": 200,
                        "metricRate": 100.0,
                        "rowCount": 40,
                        "rowRate": 20.0,
                    },
                },
            )
            for name, count, mean in (("query-1.json", 10, 2.0), ("query-2.json", 30, 4.0)):
                self.write_json(
                    run_dir,
                    f"results/{name}",
                    {
                        "ResultFormatVersion": "0.2",
                        "DurationMillis": 1000,
                        "Totals": {
                            "overallStats": {
                                "all_queries": {
                                    "count": count,
                                    "meanMilliseconds": mean,
                                }
                            }
                        },
                    },
                )
            summary = summarize.build_summary(
                run_dir,
                {
                    "events": {
                        "loads": [
                            {
                                "attempt": 1,
                                "database": "benchmark",
                                "database_mode": "create",
                                "status": "completed",
                                "log": "logs/missing-load.log",
                                "results": "results/load.json",
                            }
                        ],
                        "queries": [
                            {
                                "query_type": "lastpoint",
                                "attempt": attempt,
                                "database": "benchmark",
                                "status": "completed",
                                "log": f"logs/missing-query-{attempt}.log",
                                "results": f"results/query-{attempt}.json",
                            }
                            for attempt in (1, 2)
                        ],
                    }
                },
            )

        self.assertEqual(summary["failures"], [])
        self.assertEqual(summary["ingestion_runs"][0]["metrics"], 200)
        self.assertEqual(summary["ingestion_runs"][0]["duration_seconds"], 2.0)
        self.assertEqual(summary["queries"][0]["query_count"], 40)
        self.assertEqual(summary["queries"][0]["weighted_mean_milliseconds"], 3.5)

    def test_legacy_result_uses_log_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.write_json(
                run_dir,
                "results/query.json",
                {"ResultFormatVersion": "0.1", "Totals": {}},
            )
            log = run_dir / "logs/query.log"
            log.parent.mkdir()
            log.write_text(
                "Run complete after 10 queries\nall queries:\n"
                "min: 1ms, mean: 3.50ms, max: 4ms, count: 10\n",
                encoding="utf-8",
            )
            summary = summarize.build_summary(
                run_dir,
                {
                    "events": {
                        "loads": [],
                        "queries": [
                            {
                                "query_type": "lastpoint",
                                "attempt": 1,
                                "database": "benchmark",
                                "status": "completed",
                                "log": "logs/query.log",
                                "results": "results/query.json",
                            }
                        ],
                    }
                },
            )

        self.assertEqual(summary["failures"], [])
        self.assertEqual(summary["queries"][0]["weighted_mean_milliseconds"], 3.5)

    def test_incomplete_current_result_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.write_json(
                run_dir,
                "results/query.json",
                {"ResultFormatVersion": "0.2", "Totals": {"overallStats": {}}},
            )
            summary = summarize.build_summary(
                run_dir,
                {
                    "events": {
                        "loads": [],
                        "queries": [
                            {
                                "query_type": "lastpoint",
                                "attempt": 1,
                                "database": "benchmark",
                                "status": "completed",
                                "log": "logs/query.log",
                                "results": "results/query.json",
                            }
                        ],
                    }
                },
            )

        self.assertEqual(summary["queries"], [])
        self.assertIn("all_queries", summary["failures"][0]["reason"])
        self.assertEqual(summary["failures"][0]["log"], "logs/query.log")

    def test_malformed_current_result_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            result = run_dir / "results/query.json"
            result.parent.mkdir()
            result.write_text("{not-json", encoding="utf-8")
            summary = summarize.build_summary(
                run_dir,
                {
                    "events": {
                        "loads": [],
                        "queries": [
                            {
                                "query_type": "lastpoint",
                                "attempt": 1,
                                "database": "benchmark",
                                "status": "completed",
                                "log": "logs/query.log",
                                "results": "results/query.json",
                            }
                        ],
                    }
                },
            )

        self.assertEqual(summary["queries"], [])
        self.assertIn("malformed result JSON", summary["failures"][0]["reason"])

    def test_parsers_and_target_identity(self) -> None:
        structured_load = summarize.parse_load_result(
            {
                "DurationMillis": 2000,
                "Totals": {
                    "metricCount": 200,
                    "metricRate": 100.0,
                    "rowCount": 0,
                    "rowRate": 0.0,
                },
            }
        )
        self.assertNotIn("rows", structured_load)
        load = summarize.parse_load_log(
            "loaded 200 metrics in 2.000sec (mean rate 100.00 metrics/sec)\n"
            "loaded 40 rows in 2.000sec (mean rate 20.00 rows/sec)\n"
        )
        self.assertEqual(load["metrics_per_second"], 100.0)
        query = summarize.parse_query_log(
            "Run complete after 10 queries\nall queries:\n"
            "min: 1ms, mean: 3.50ms, max: 4ms, count: 10\n"
        )
        self.assertEqual(query, {"mean_milliseconds": 3.5, "count": 10})
        with tempfile.TemporaryDirectory() as temp:
            summary = summarize.build_summary(
                Path(temp),
                {
                    "run_id": "run", "profile": "smoke", "database": "benchmark",
                    "target": {"mode": "managed", "database_id": "db-a"},
                    "dataset": {"dataset_id": "data-a"},
                    "query_set": {"query_set_id": "set-a", "manifest_sha256": "abc"},
                    "events": {"loads": [], "queries": []},
                },
            )
        self.assertEqual(summary["target"]["database_id"], "db-a")
        self.assertEqual(summary["query_set"]["query_set_id"], "set-a")
        rendered = summarize.render_markdown(summary)
        self.assertIn("managed:db-a", rendered)
        self.assertIn("set-a", rendered)


class QuerySetIdentityTests(unittest.TestCase):
    def dataset(self) -> dict:
        return {"dataset_id": "data-a", "spec": {"use_case": "cpu-only", "seed": 123}}

    def workload(self, query_counts: dict[str, int]) -> dict:
        return {
            "seed": 123, "scale": 10, "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-02T00:00:00Z", "query_counts": query_counts,
        }

    def test_identity_is_canonical_and_membership_sensitive(self) -> None:
        first = benchmark.query_set_spec(self.dataset(), self.workload({"lastpoint": 3, "cpu-max-all-1": 2}))
        reordered = benchmark.query_set_spec(self.dataset(), self.workload({"cpu-max-all-1": 2, "lastpoint": 3}))
        self.assertEqual(benchmark.query_set_id(first), benchmark.query_set_id(reordered))
        changed_count = benchmark.query_set_spec(self.dataset(), self.workload({"cpu-max-all-1": 3, "lastpoint": 3}))
        subset = benchmark.query_set_spec(self.dataset(), self.workload({"lastpoint": 3}))
        self.assertNotEqual(benchmark.query_set_id(first), benchmark.query_set_id(changed_count))
        self.assertNotEqual(benchmark.query_set_id(first), benchmark.query_set_id(subset))


class WorkspaceTests(unittest.TestCase):
    def test_new_run_layout_has_no_local_artifacts_or_managed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = benchmark.make_parser().parse_args(["generate", "--run-root", str(root), "--only", "queries", "--profile", "smoke"])
            run_dir, manifest = benchmark.prepare_run(args)
            self.assertEqual(manifest["schema_version"], benchmark.SCHEMA_VERSION)
            self.assertEqual({path.name for path in run_dir.iterdir()}, {"logs", "results", "manifest.json"})
            self.assertFalse((run_dir / "queries").exists())
            self.assertFalse((run_dir / "data").exists())
            self.assertFalse((run_dir / "greptimedb").exists())

    def test_old_or_malformed_run_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "manifest.json").write_text(json.dumps({"run_id": "old"}), encoding="utf-8")
            args = benchmark.make_parser().parse_args(["generate", "--run-dir", str(run_dir), "--only", "queries"])
            with self.assertRaisesRegex(benchmark.BenchmarkError, "unsupported"):
                benchmark.prepare_run(args)


class QuerySetTests(unittest.TestCase):
    def make_args(self, run_dir: Path, query_root: Path, *types: str) -> argparse.Namespace:
        values = ["generate", "--run-dir", str(run_dir), "--query-root", str(query_root), "--profile", "smoke", "--only", "queries"]
        for query_type in types:
            values.extend(["--query-type", query_type])
        return benchmark.make_parser().parse_args(values)

    def make_manifest(self, run_dir: Path, args: argparse.Namespace) -> dict:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(); (run_dir / "results").mkdir()
        manifest = {
            "schema_version": benchmark.SCHEMA_VERSION, "kind": "greptimedb-run",
            "run_id": run_dir.name, "created_at": benchmark.utc_now(), "profile": "smoke",
            "database": "benchmark", "workload": benchmark.build_workload(args),
            "dataset": {"dataset_id": "data-a", "dataset_path": "/tmp/data-a", "spec": {
                "use_case": "cpu-only", "start": "2023-06-11T00:00:00Z", "end": "2023-06-12T00:00:00Z",
                "scale": 10, "seed": 123, "log_interval": "10s",
            }},
            "events": {"loads": [], "queries": []},
        }
        benchmark.save_manifest(run_dir, manifest)
        return manifest

    def generator(self, _command, log_path, *, stdout_path, **_kwargs):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("generated\n", encoding="utf-8")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(f"query:{stdout_path.stem}\n", encoding="utf-8")

    def checksum(self, path: Path) -> str:
        if path.name == benchmark.BINARIES["queries"]:
            return "b" * 64
        return benchmark._real_sha(path) if hasattr(benchmark, "_real_sha") else ""

    def test_independent_runs_reuse_identical_validated_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); query_root = root / "queries"
            real_sha = benchmark.sha256_file
            def hashes(path): return "b" * 64 if path.name == benchmark.BINARIES["queries"] else real_sha(path)
            paths = []
            for name in ("run-a", "run-b"):
                run_dir = root / name; args = self.make_args(run_dir, query_root, "lastpoint", "cpu-max-all-1"); manifest = self.make_manifest(run_dir, args)
                with mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee", side_effect=self.generator) as runner, mock.patch.object(benchmark, "sha256_file", side_effect=hashes):
                    paths.append(benchmark.generate_queries(args, run_dir, manifest))
                if name == "run-a": self.assertEqual(runner.call_count, 2)
                else: runner.assert_not_called()
            self.assertEqual(paths[0], paths[1])
            self.assertEqual(json.loads((root / "run-b/manifest.json").read_text())["query_set"]["reused"], True)

    def test_failure_is_atomic_and_diagnostics_stay_in_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run_dir = root / "run"; query_root = root / "queries"
            args = self.make_args(run_dir, query_root, "lastpoint", "cpu-max-all-1"); manifest = self.make_manifest(run_dir, args)
            calls = 0
            def fail_second(command, log_path, *, stdout_path, **kwargs):
                nonlocal calls; calls += 1; log_path.write_text("generator failed\n", encoding="utf-8")
                if calls == 2: raise benchmark.BenchmarkError("failed")
                stdout_path.parent.mkdir(parents=True, exist_ok=True); stdout_path.write_text("partial", encoding="utf-8")
            with mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee", side_effect=fail_second):
                with self.assertRaises(benchmark.BenchmarkError): benchmark.generate_queries(args, run_dir, manifest)
            spec = benchmark.query_set_spec(manifest["dataset"], manifest["workload"])
            self.assertFalse(benchmark.query_set_path(query_root, "data-a", benchmark.query_set_id(spec)).exists())
            self.assertTrue(any((run_dir / "logs").iterdir()))
            self.assertFalse(any(query_root.rglob("*.dat")))

    def test_corrupt_manifest_or_artifact_fails_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run_dir = root / "run"; query_root = root / "queries"
            args = self.make_args(run_dir, query_root, "lastpoint"); manifest = self.make_manifest(run_dir, args); real_sha = benchmark.sha256_file
            def hashes(path): return "b" * 64 if path.name == benchmark.BINARIES["queries"] else real_sha(path)
            with mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee", side_effect=self.generator), mock.patch.object(benchmark, "sha256_file", side_effect=hashes):
                path = benchmark.generate_queries(args, run_dir, manifest)
            benchmark.query_file_path(path, "lastpoint").write_text("corrupt", encoding="utf-8")
            with self.assertRaisesRegex(benchmark.BenchmarkError, "checksum mismatch"):
                benchmark.validate_query_set(path, manifest["query_set"]["spec"])

    def test_each_query_file_executes_once_and_records_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run_dir = root / "run"; query_root = root / "queries"
            args = self.make_args(run_dir, query_root, "lastpoint", "cpu-max-all-1"); args.database = "benchmark"; manifest = self.make_manifest(run_dir, args); real_sha = benchmark.sha256_file
            def hashes(path): return "b" * 64 if path.name == benchmark.BINARIES["queries"] else real_sha(path)
            with mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee", side_effect=self.generator), mock.patch.object(benchmark, "sha256_file", side_effect=hashes):
                benchmark.generate_queries(args, run_dir, manifest)
            def execute(_command, log_path, **_kwargs):
                log_path.write_text("Run complete after 1 queries\nall queries:\nmin: 1ms, mean: 1ms, max: 1ms, count: 1\n", encoding="utf-8")
            with mock.patch.object(benchmark, "generate_queries", return_value=Path(manifest["query_set"]["query_set_path"])), mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee", side_effect=execute) as runner:
                benchmark.run_queries(args, run_dir, manifest, "http://localhost:4000")
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(len(manifest["events"]["queries"]), 2)
            self.assertTrue(all(event["file_sha256"] for event in manifest["events"]["queries"]))


class ManagedDatabaseTests(unittest.TestCase):
    def args(self, root: Path, mode: str | None = None) -> argparse.Namespace:
        values = ["load", "--greptime-bin", "/bin/true", "--database-id", "db-a", "--database-root", str(root), "--database", "benchmark"]
        if mode: values.extend(["--database-mode", mode])
        if mode == "reset": values.extend(["--confirm-reset", "benchmark"])
        return benchmark.make_parser().parse_args(values)

    def test_workspace_bind_reuse_reset_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); args = self.args(root); path, db = benchmark.prepare_database_workspace(args)
            self.assertEqual({p.name for p in path.iterdir()}, {"manifest.json", "data", "logs"})
            with benchmark.lock_database(path):
                with self.assertRaisesRegex(benchmark.BenchmarkError, "locked"):
                    with benchmark.lock_database(path): pass
            dataset_a = {"dataset_id": "a", "dataset_path": "/a", "data_path": "/a/data", "format": "influx", "bytes": 1, "sha256": "a", "spec": {"use_case": "cpu-only"}}
            dataset_b = {**dataset_a, "dataset_id": "b", "sha256": "b"}
            run_dir = root / "run"; (run_dir / "logs").mkdir(parents=True); (run_dir / "results").mkdir()
            manifest = {"workload": {"batch_size": 1, "load_workers": 1}, "events": {"loads": [], "queries": []}, "dataset": dataset_a}
            with mock.patch.object(benchmark, "generate_data", return_value=Path("/a/data")), mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee"):
                benchmark.load_data(args, run_dir, manifest, "http://localhost", True, db, path)
            db = benchmark.validate_database_manifest(path, "db-a"); self.assertEqual(db["binding"]["dataset_id"], "a")
            manifest["events"] = {"loads": [], "queries": []}
            with mock.patch.object(benchmark, "generate_data", return_value=Path("/a/data")), mock.patch.object(benchmark, "ensure_binaries") as build, mock.patch.object(benchmark, "run_tee"):
                benchmark.load_data(args, run_dir, manifest, "http://localhost", True, db, path)
            build.assert_not_called(); self.assertEqual(manifest["events"]["loads"][0]["status"], "reused")
            manifest["dataset"] = dataset_b; manifest["events"] = {"loads": [], "queries": []}
            with mock.patch.object(benchmark, "generate_data", return_value=Path("/b/data")):
                with self.assertRaisesRegex(benchmark.BenchmarkError, "different dataset"):
                    benchmark.load_data(args, run_dir, manifest, "http://localhost", True, db, path)
            reset = self.args(root, "reset")
            with mock.patch.object(benchmark, "generate_data", return_value=Path("/b/data")), mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee"):
                benchmark.load_data(reset, run_dir, manifest, "http://localhost", True, db, path)
            self.assertEqual(benchmark.validate_database_manifest(path)["binding"]["dataset_id"], "b")

    def test_managed_requires_database_id(self) -> None:
        args = benchmark.make_parser().parse_args(["query", "--greptime-bin", "/bin/true"])
        benchmark.resolve_database(args)
        with self.assertRaisesRegex(benchmark.BenchmarkError, "database-id"):
            benchmark.validate_args(args)


if __name__ == "__main__":
    unittest.main()
