from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import benchmark  # noqa: E402
import compare as version_compare  # noqa: E402
import summarize  # noqa: E402


class SummaryIntegrationTests(unittest.TestCase):
    def test_greptimedb_target_identity_is_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            summary = summarize.build_summary(
                Path(temp),
                {
                    "run_id": "run",
                    "profile": "smoke",
                    "database": "benchmark",
                    "target": {"mode": "managed", "database_id": "db-a", "version": "1.1.4", "binary_sha256": "def"},
                    "dataset": {"dataset_id": "data-a"},
                    "query_set": {
                        "query_set_id": "set-a",
                        "manifest_sha256": "abc",
                    },
                    "events": {"loads": [], "queries": []},
                },
            )

        rendered = summarize.render_markdown(summary)
        self.assertIn("managed:db-a", rendered)
        self.assertIn("GreptimeDB version: `1.1.4`", rendered)
        self.assertIn("GreptimeDB binary SHA-256: `def`", rendered)
        self.assertIn("set-a", rendered)

    def test_version_override_identity_is_rendered(self) -> None:
        summary = {
            "run_id": "run", "profile": "smoke", "database": "benchmark",
            "target": {
                "mode": "managed", "database_id": "db-a", "version": "1.0.0",
                "binary_sha256": "old", "workspace_version": "1.1.4",
                "workspace_binary_sha256": "new", "version_override": True,
            },
            "ingestion_runs": [], "queries": [], "failures": [],
        }
        rendered = summarize.render_markdown(summary)
        self.assertIn("Runtime GreptimeDB version: `1.0.0`", rendered)
        self.assertIn("Workspace-bound GreptimeDB version: `1.1.4`", rendered)


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


class VersionComparisonTests(unittest.TestCase):
    def make_run(self, root: Path, name: str, version: str, means: dict[str, float], *, dataset_id: str = "data-a") -> Path:
        run_dir = root / name; (run_dir / "results").mkdir(parents=True); (run_dir / "logs").mkdir()
        query_counts = {query_type: 10 for query_type in means}
        events = []
        for query_type, mean in means.items():
            result = run_dir / "results" / f"{query_type}.json"
            result.write_text(json.dumps({"ResultFormatVersion": "0.2", "Totals": {"overallStats": {"all_queries": {"meanMilliseconds": mean, "count": 10}}}}), encoding="utf-8")
            log = run_dir / "logs" / f"{query_type}.log"; log.write_text("completed\n", encoding="utf-8")
            events.append({
                "query_type": query_type, "attempt": 1, "database": "benchmark",
                "log": f"logs/{query_type}.log", "results": f"results/{query_type}.json",
                "status": "completed",
            })
        manifest = {
            "schema_version": 1, "kind": "greptimedb-run", "run_id": name,
            "created_at": benchmark.utc_now(), "profile": "manual", "database": "benchmark",
            "workload": {"query_counts": query_counts},
            "target": {"mode": "managed", "database_id": f"db-{version}", "version": version, "binary_sha256": version * 4},
            "dataset": {"dataset_id": dataset_id, "spec": {"scale": 10}, "format": "influx", "bytes": 100, "sha256": "d" * 64},
            "query_set": {"query_set_id": "set-a", "manifest_sha256": "q" * 64, "spec": {"query_counts": query_counts}},
            "events": {"loads": [], "queries": events},
        }
        benchmark.save_json(run_dir / "manifest.json", manifest)
        return run_dir

    def test_compares_saved_runs_and_writes_immutable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self.make_run(root, "baseline", "1.1.4", {"lastpoint": 10.0, "high-cpu-1": 20.0})
            candidate = self.make_run(root, "candidate", "1.0.0", {"lastpoint": 12.0, "high-cpu-1": 10.0})
            output = version_compare.create_comparison(baseline, [candidate], root / "comparisons")
            summary = json.loads((output / "summary.json").read_text())
            by_type = {item["query_type"]: item for item in summary["candidates"][0]["queries"]}
            self.assertAlmostEqual(by_type["lastpoint"]["delta_percent"], 20.0)
            self.assertEqual(by_type["lastpoint"]["classification"], "regressed")
            self.assertEqual(by_type["high-cpu-1"]["classification"], "improved")
            self.assertEqual(summary["candidates"][0]["counts"], {"improved": 1, "unchanged": 0, "regressed": 1})
            self.assertTrue((output / "manifest.json").is_file())
            self.assertIn("Latency ratio", (output / "summary.md").read_text())
            second = version_compare.create_comparison(baseline, [candidate], root / "comparisons")
            self.assertNotEqual(output, second)

    def test_rejects_mismatched_or_incomplete_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self.make_run(root, "baseline", "1.1.4", {"lastpoint": 10.0})
            mismatch = self.make_run(root, "mismatch", "1.0.0", {"lastpoint": 12.0}, dataset_id="data-b")
            with self.assertRaisesRegex(version_compare.ComparisonError, "does not match"):
                version_compare.create_comparison(baseline, [mismatch], root / "comparisons")
            incomplete = self.make_run(root, "incomplete", "1.0.0", {"lastpoint": 12.0})
            manifest = json.loads((incomplete / "manifest.json").read_text())
            manifest["events"]["queries"][0]["status"] = "failed"
            benchmark.save_json(incomplete / "manifest.json", manifest)
            with self.assertRaisesRegex(version_compare.ComparisonError, "failures"):
                version_compare.create_comparison(baseline, [incomplete], root / "comparisons")

    def test_zero_baseline_latency_has_no_infinite_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline = self.make_run(root, "baseline", "1.1.4", {"lastpoint": 0.0})
            candidate = self.make_run(root, "candidate", "1.0.0", {"lastpoint": 1.0})
            output = version_compare.create_comparison(baseline, [candidate], root / "comparisons")
            query = json.loads((output / "summary.json").read_text())["candidates"][0]["queries"][0]
            self.assertIsNone(query["delta_percent"])
            self.assertIsNone(query["latency_ratio"])


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

    def test_per_type_count_change_is_rejected_for_an_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parser = benchmark.make_parser()
            initial = parser.parse_args([
                "generate", "--run-root", str(root), "--only", "queries",
                "--query-count", "lastpoint=7",
            ])
            run_dir, _ = benchmark.prepare_run(initial)
            changed = parser.parse_args([
                "generate", "--run-dir", str(run_dir), "--only", "queries",
                "--query-count", "lastpoint=8",
            ])

            with self.assertRaisesRegex(benchmark.BenchmarkError, "immutable"):
                benchmark.prepare_run(changed)


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

    def test_generator_receives_each_per_type_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run_dir = root / "run"; query_root = root / "queries"
            args = benchmark.make_parser().parse_args([
                "generate", "--run-dir", str(run_dir), "--query-root", str(query_root),
                "--profile", "smoke", "--only", "queries",
                "--query-count", "lastpoint=7",
                "--query-count", "cpu-max-all-1=23",
            ])
            manifest = self.make_manifest(run_dir, args)
            real_sha = benchmark.sha256_file

            def hashes(path):
                return "b" * 64 if path.name == benchmark.BINARIES["queries"] else real_sha(path)

            with mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(
                benchmark, "run_tee", side_effect=self.generator
            ) as runner, mock.patch.object(benchmark, "sha256_file", side_effect=hashes):
                benchmark.generate_queries(args, run_dir, manifest)

            commands = [call.args[0] for call in runner.call_args_list]
            counts_by_type = {
                next(part.removeprefix("--query-type=") for part in command if part.startswith("--query-type=")):
                next(part.removeprefix("--queries=") for part in command if part.startswith("--queries="))
                for command in commands
            }
            self.assertEqual(counts_by_type, {"cpu-max-all-1": "23", "lastpoint": "7"})

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
            binding = {
                "dataset_id": manifest["dataset"]["dataset_id"], "spec": manifest["dataset"]["spec"],
                "format": "influx", "bytes": 123, "sha256": "d" * 64,
            }
            with mock.patch.object(benchmark, "generate_queries", return_value=Path(manifest["query_set"]["query_set_path"])), mock.patch.object(benchmark, "ensure_binaries"), mock.patch.object(benchmark, "run_tee", side_effect=execute) as runner:
                benchmark.run_queries(args, run_dir, manifest, "http://localhost:4000", {"binding": binding})
            self.assertEqual(runner.call_count, 2)
            self.assertEqual(len(manifest["events"]["queries"]), 2)
            self.assertTrue(all(event["file_sha256"] for event in manifest["events"]["queries"]))
            self.assertEqual(manifest["dataset"]["sha256"], "d" * 64)


class AnalyzeTests(unittest.TestCase):
    def args(self, run_dir: Path, hot_runs: int = 2) -> argparse.Namespace:
        return benchmark.make_parser().parse_args([
            "analyze", "--run-dir", str(run_dir), "--database-id", "db-a",
            "--database", "benchmark", "--profile", "smoke", "--hot-runs", str(hot_runs),
        ])

    def manifest(self, query_counts: dict[str, int]) -> dict:
        return {
            "schema_version": benchmark.SCHEMA_VERSION,
            "kind": "greptimedb-run",
            "run_id": "run",
            "created_at": benchmark.utc_now(),
            "profile": "smoke",
            "database": "benchmark",
            "workload": {"query_counts": query_counts},
            "dataset": {"dataset_id": "data-a", "spec": {"scale": 10}},
            "query_set": {"query_set_id": "set-a", "spec": {"query_counts": query_counts}},
            "events": {"loads": [], "queries": [], "analyses": []},
        }

    def set_manifest(self, query_counts: dict[str, int]) -> dict:
        return {
            "query_set_id": "set-a",
            "spec": {"query_counts": query_counts},
            "files": {
                query_type: {
                    "path": f"queries/{query_type}.dat",
                    "bytes": 100,
                    "sha256": query_type.ljust(64, "0")[:64],
                }
                for query_type in query_counts
            },
        }

    def execute(self, command, log_path, **_kwargs):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("analysis completed\n", encoding="utf-8")
        options = {part.split("=", 1)[0]: part.split("=", 1)[1] for part in command if part.startswith("--") and "=" in part}
        result_dir = Path(options["--explain-results-dir"])
        result_dir.mkdir(parents=True)
        count = int(options["--max-queries"])
        for index in range(count):
            phase = "cold" if index == 0 else "hot"
            name = "cold.json" if index == 0 else f"hot-{index:03d}.json"
            sql = f"SELECT {index}"
            benchmark.save_json(result_dir / name, {
                "phase": phase,
                "query_index": index,
                "sql": sql,
                "executed_sql": "EXPLAIN ANALYZE VERBOSE " + sql,
                "response": {"output": [], "execution_time_ms": index},
            })
        benchmark.save_json(Path(options["--results-file"]), {"ResultFormatVersion": "0.2"})

    def test_analyze_restarts_each_type_and_writes_attempt_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"; (run_dir / "logs").mkdir(parents=True); (run_dir / "results").mkdir()
            query_counts = {"lastpoint": 3, "cpu-max-all-1": 3}
            manifest = self.manifest(query_counts)
            args = self.args(run_dir)
            starts = []

            @contextlib.contextmanager
            def process(_args, _workspace, _binary, log_path):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("server started\n", encoding="utf-8")
                starts.append(log_path)
                yield "http://127.0.0.1:4000"

            patches = (
                mock.patch.object(benchmark, "generate_queries", return_value=Path(temp) / "queries"),
                mock.patch.object(benchmark, "validate_query_set", return_value=self.set_manifest(query_counts)),
                mock.patch.object(benchmark, "validate_query_database_binding"),
                mock.patch.object(benchmark, "ensure_binaries"),
                mock.patch.object(benchmark, "managed_process", side_effect=process),
                mock.patch.object(benchmark, "run_tee", side_effect=self.execute),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as runner:
                benchmark.run_analyses(args, run_dir, manifest, Path(temp), {"binding": {}}, Path("/greptime"))
                benchmark.run_analyses(args, run_dir, manifest, Path(temp), {"binding": {}}, Path("/greptime"))

            self.assertEqual(len(starts), 4)
            self.assertEqual(runner.call_count, 4)
            self.assertEqual([event["attempt"] for event in manifest["events"]["analyses"]], [1, 1, 2, 2])
            for query_type in query_counts:
                for attempt in (1, 2):
                    result_dir = run_dir / "results/analyze" / query_type / f"run-{attempt:03d}"
                    self.assertTrue((result_dir / "cold.json").is_file())
                    self.assertTrue((result_dir / "hot-001.json").is_file())
                    self.assertTrue((result_dir / "hot-002.json").is_file())
                    self.assertTrue((result_dir / "metrics.json").is_file())
            command = runner.call_args_list[0].args[0]
            self.assertIn("--workers=1", command)
            self.assertIn("--max-queries=3", command)
            self.assertIn("--explain-analyze-verbose", command)

            summary = summarize.build_summary(run_dir, manifest)
            self.assertEqual(len(summary["analyses"]), 4)
            self.assertFalse(summary["failures"])
            self.assertIn("## Analysis", summarize.render_markdown(summary))

    def test_analyze_rejects_insufficient_distinct_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"; (run_dir / "logs").mkdir(parents=True); (run_dir / "results").mkdir()
            query_counts = {"lastpoint": 2}
            manifest = self.manifest(query_counts)
            args = self.args(run_dir)
            with mock.patch.object(benchmark, "generate_queries", return_value=Path(temp) / "queries"), mock.patch.object(
                benchmark, "validate_query_set", return_value=self.set_manifest(query_counts)
            ), mock.patch.object(benchmark, "validate_query_database_binding"), mock.patch.object(
                benchmark, "ensure_binaries"
            ) as build:
                with self.assertRaisesRegex(benchmark.BenchmarkError, "at least 3"):
                    benchmark.run_analyses(args, run_dir, manifest, Path(temp), {"binding": {}}, Path("/greptime"))
            build.assert_not_called()

    def test_analyze_cli_is_managed_only_and_hot_runs_are_positive(self) -> None:
        parser = benchmark.make_parser()
        external = parser.parse_args(["analyze", "--endpoint", "http://localhost:4000"])
        benchmark.resolve_database(external)
        with self.assertRaisesRegex(benchmark.BenchmarkError, "managed"):
            benchmark.validate_args(external)
        invalid = parser.parse_args(["analyze", "--database-id", "db-a", "--hot-runs", "0"])
        benchmark.resolve_database(invalid)
        with self.assertRaisesRegex(benchmark.BenchmarkError, "positive"):
            benchmark.validate_args(invalid)

    def test_port_availability_retries_between_restarts(self) -> None:
        with mock.patch.object(benchmark, "port_available", side_effect=[False, False, True]) as available, mock.patch.object(
            benchmark.time, "sleep"
        ) as sleep:
            benchmark.check_port_available(4000)
        self.assertEqual(available.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


class BuildEnvironmentTests(unittest.TestCase):
    def test_build_uses_resolved_go_and_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run_dir = root / "run"
            (run_dir / "results").mkdir(parents=True); (run_dir / "logs").mkdir()
            toolchain = {"source": "managed", "version": "1.21.13", "binary": "/managed/go", "binary_sha256": "a" * 64}

            def build(command, _log_path, **_kwargs):
                target = Path(command[3]); target.parent.mkdir(parents=True, exist_ok=True); target.write_text("binary", encoding="utf-8")

            with mock.patch.object(benchmark, "REPO_ROOT", root), mock.patch.object(benchmark, "resolve_go", return_value=toolchain), mock.patch.object(
                benchmark, "run_tee", side_effect=build
            ) as runner, mock.patch.object(benchmark, "GO_TOOLCHAIN", None), mock.patch.object(benchmark, "BUILT_THIS_PROCESS", set()):
                built = benchmark.ensure_binaries(run_dir, ["queries"], False)
            self.assertEqual(runner.call_args.args[0][0], "/managed/go")
            self.assertEqual(built[benchmark.BINARIES["queries"]]["go_toolchain"], toolchain)
            marker = json.loads((run_dir / "results" / f"built-{benchmark.BINARIES['queries']}").read_text(encoding="utf-8"))
            self.assertEqual(marker["go_toolchain"]["source"], "managed")
            self.assertIn('"version": "1.21.13"', (run_dir / "logs" / "build.log").read_text(encoding="utf-8"))


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

    def test_prepared_workspace_discovers_and_validates_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); installation = root / "installations/1.1.4/linux_amd64"; installation.mkdir(parents=True)
            binary = installation / "greptime"; binary.write_text("#!/bin/sh\n", encoding="utf-8"); binary.chmod(0o755)
            database = root / "databases/db-a"; (database / "data").mkdir(parents=True); (database / "logs").mkdir()
            benchmark.save_json(database / "manifest.json", {
                "schema_version": 1, "kind": "greptimedb-database", "database_id": "db-a",
                "created_at": benchmark.utc_now(), "database": "benchmark", "binding": None,
                "version": "1.1.4", "version_source": "explicit", "platform": "linux_amd64",
                "installation_path": str(installation), "binary_sha256": benchmark.sha256_file(binary),
            })
            args = benchmark.make_parser().parse_args(["query", "--database-id", "db-a", "--database-root", str(root / "databases"), "--database", "benchmark"])
            manifest = benchmark.validate_database_manifest(database, "db-a")
            self.assertEqual(benchmark.managed_binary(args, manifest), binary.resolve())
            explicit = benchmark.make_parser().parse_args(["query", "--greptime-bin", "/bin/true", "--database-id", "db-a", "--database-root", str(root / "databases"), "--database", "benchmark"])
            with self.assertRaisesRegex(benchmark.BenchmarkError, "conflicts"):
                benchmark.managed_binary(explicit, manifest)
            binary.write_text("corrupt", encoding="utf-8")
            with self.assertRaisesRegex(benchmark.BenchmarkError, "checksum mismatch"):
                benchmark.validate_database_manifest(database, "db-a")

    def test_legacy_workspace_requires_explicit_binary(self) -> None:
        manifest = {"database_id": "db-a", "database": "benchmark", "binding": None}
        args = benchmark.make_parser().parse_args(["query", "--database-id", "db-a", "--database", "benchmark"])
        with self.assertRaisesRegex(benchmark.BenchmarkError, "legacy managed workspace"):
            benchmark.managed_binary(args, manifest)

    def test_missing_prepared_workspace_does_not_create_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); args = benchmark.make_parser().parse_args(["query", "--database-id", "db-a", "--database-root", str(root), "--database", "benchmark"])
            with self.assertRaisesRegex(benchmark.BenchmarkError, "does not exist"):
                benchmark.prepare_database_workspace(args)
            self.assertFalse((root / "db-a").exists())

    def test_query_can_use_confirmed_installed_version_without_rebinding_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bound_installation = root / "installations/1.1.4/linux_amd64"; bound_installation.mkdir(parents=True)
            bound_binary = bound_installation / "greptime"; bound_binary.write_text("#!/bin/sh\necho 'greptime 1.1.4'\n", encoding="utf-8"); bound_binary.chmod(0o755)
            alternate = root / "installations/1.0.0/linux_amd64"; alternate.mkdir(parents=True)
            alternate_binary = alternate / "greptime"; alternate_binary.write_text("#!/bin/sh\necho 'greptime 1.0.0'\n", encoding="utf-8"); alternate_binary.chmod(0o755)
            benchmark.save_json(alternate / "manifest.json", {
                "schema_version": 1, "kind": "greptimedb-installation", "version": "1.0.0",
                "platform": "linux_amd64", "binary": "greptime",
                "binary_sha256": benchmark.sha256_file(alternate_binary),
            })
            database = root / "databases/db-a"; (database / "data").mkdir(parents=True); (database / "logs").mkdir()
            original = {
                "schema_version": 1, "kind": "greptimedb-database", "database_id": "db-a",
                "created_at": benchmark.utc_now(), "database": "benchmark", "binding": None,
                "version": "1.1.4", "version_source": "explicit", "platform": "linux_amd64",
                "installation_path": str(bound_installation), "binary_sha256": benchmark.sha256_file(bound_binary),
            }
            benchmark.save_json(database / "manifest.json", original)
            args = benchmark.make_parser().parse_args([
                "query", "--database-id", "db-a", "--database-root", str(root / "databases"),
                "--database", "benchmark", "--greptime-version", "1.0.0",
                "--install-root", str(root / "installations"), "--confirm-version-override", "db-a",
            ])
            manifest = benchmark.validate_database_manifest(database, "db-a")
            binary = benchmark.managed_binary(args, manifest)
            target = benchmark.managed_target(args, manifest, binary)
            self.assertEqual(binary, alternate_binary.resolve())
            self.assertEqual(target["version"], "1.0.0")
            self.assertEqual(target["workspace_version"], "1.1.4")
            self.assertTrue(target["version_override"])
            self.assertEqual(json.loads((database / "manifest.json").read_text()), original)

            args.confirm_version_override = "wrong"
            with self.assertRaisesRegex(benchmark.BenchmarkError, "exactly match"):
                benchmark.managed_binary(args, manifest)

    def test_version_override_is_query_only_and_managed_only(self) -> None:
        parser = benchmark.make_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["load", "--database-id", "db-a", "--greptime-version", "1.0.0"])
        args = parser.parse_args(["query", "--endpoint", "http://localhost:4000", "--greptime-version", "1.0.0"])
        benchmark.resolve_database(args)
        with self.assertRaisesRegex(benchmark.BenchmarkError, "external GreptimeDB"):
            benchmark.validate_args(args)


if __name__ == "__main__":
    unittest.main()
