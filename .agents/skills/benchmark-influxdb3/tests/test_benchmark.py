from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import benchmark  # noqa: E402
import summarize  # noqa: E402


class SummaryIntegrationTests(unittest.TestCase):
    def test_server_warnings_are_diagnostics_but_fatal_output_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "server-1.log").write_text(
                " WARN retrying for person@example.com\n ERROR request recovered\n"
            )
            manifest = {
                "events": {
                    "loads": [],
                    "queries": [],
                    "servers": [
                        {
                            "attempt": 1,
                            "log": "logs/server-1.log",
                            "status": "stopped",
                        }
                    ],
                }
            }
            summary = summarize.build_summary(run_dir, manifest)
            self.assertEqual(summary["failures"], [])
            self.assertEqual(summary["server_diagnostics"]["warning_count"], 1)
            self.assertEqual(summary["server_diagnostics"]["error_count"], 1)
            self.assertNotIn("person@example.com", json.dumps(summary))

            (logs / "server-1.log").write_text(
                "thread worker panicked at secret@example.com\n"
            )
            failed = summarize.build_summary(run_dir, manifest)
            self.assertEqual(failed["failures"][0]["stage"], "server")
            self.assertEqual(failed["server_diagnostics"]["panic_count"], 1)

    def test_server_lifecycle_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "server.log").write_text("startup ended\n")
            summary = summarize.build_summary(
                run_dir,
                {
                    "events": {
                        "loads": [],
                        "queries": [],
                        "servers": [
                            {
                                "attempt": 1,
                                "log": "logs/server.log",
                                "status": "unexpected_exit",
                                "unexpected_exit": True,
                            }
                        ],
                    }
                },
            )

        self.assertEqual(summary["failures"][0]["stage"], "server")

    def test_influxdb3_target_identity_is_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            summary = summarize.build_summary(
                Path(temp),
                {
                    "run_id": "run",
                    "profile": "smoke",
                    "database": "benchmark",
                    "target": {
                        "mode": "managed",
                        "database_id": "db-a",
                        "edition": "core",
                        "urls": ["http://localhost:8181"],
                    },
                    "dataset": {"dataset_id": "data-a"},
                    "query_set": {
                        "query_set_id": "set-a",
                        "manifest_sha256": "abc",
                    },
                    "events": {"loads": [], "queries": [], "servers": []},
                },
            )

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
    def test_manual_load_defaults_are_influxdb_specific_and_overridable(self) -> None:
        parser = benchmark.make_parser()
        manual = parser.parse_args(["generate", "--only", "data"])
        smoke = parser.parse_args(["generate", "--only", "data", "--profile", "smoke"])
        overridden = parser.parse_args([
            "generate", "--only", "data", "--batch-size", "7000", "--load-workers", "3",
        ])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manual.run_root = root / "manual"
            smoke.run_root = root / "smoke"
            overridden.run_root = root / "overridden"
            manual_dir, manual_manifest = benchmark.prepare_run(manual)
            _, smoke_manifest = benchmark.prepare_run(smoke)
            _, overridden_manifest = benchmark.prepare_run(overridden)
            reopened = parser.parse_args([
                "generate", "--run-dir", str(manual_dir), "--only", "data",
                "--profile", "manual",
            ])
            _, reopened_manifest = benchmark.prepare_run(reopened)

        self.assertEqual(manual_manifest["workload"]["batch_size"], 25000)
        self.assertEqual(manual_manifest["workload"]["load_workers"], 16)
        self.assertEqual(reopened_manifest["workload"], manual_manifest["workload"])
        self.assertEqual(benchmark.build_workload(manual)["batch_size"], 3000)
        self.assertEqual(benchmark.build_workload(manual)["load_workers"], 6)
        self.assertEqual(smoke_manifest["workload"]["batch_size"], 3000)
        self.assertEqual(smoke_manifest["workload"]["load_workers"], 2)
        self.assertEqual(overridden_manifest["workload"]["batch_size"], 7000)
        self.assertEqual(overridden_manifest["workload"]["load_workers"], 3)

    def test_port_probe_enables_address_reuse_and_retries(self) -> None:
        sock = mock.MagicMock(); sock.__enter__.return_value = sock
        with mock.patch.object(benchmark.socket, "socket", return_value=sock):
            self.assertTrue(benchmark.port_available(8181))
        sock.setsockopt.assert_called_once_with(benchmark.socket.SOL_SOCKET, benchmark.socket.SO_REUSEADDR, 1)
        with mock.patch.object(benchmark, "port_available", side_effect=[False, True]), mock.patch.object(benchmark.time, "sleep"):
            benchmark.check_port_available(8181)

    def test_new_run_layout_has_no_local_artifacts_or_managed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = benchmark.make_parser().parse_args(["generate", "--run-root", str(root), "--only", "queries", "--profile", "smoke"])
            run_dir, manifest = benchmark.prepare_run(args)
            self.assertEqual(manifest["schema_version"], benchmark.SCHEMA_VERSION)
            self.assertEqual({path.name for path in run_dir.iterdir()}, {"logs", "results", "manifest.json"})
            self.assertFalse((run_dir / "queries").exists())
            self.assertFalse((run_dir / "data").exists())
            self.assertFalse((run_dir / "influxdb3").exists())

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
            "schema_version": benchmark.SCHEMA_VERSION, "kind": "influxdb3-run",
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


class BuildEnvironmentTests(unittest.TestCase):
    def test_build_uses_resolved_go_and_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); run_dir = root / "run"
            (run_dir / "results").mkdir(parents=True); (run_dir / "logs").mkdir()
            toolchain = {"source": "system", "version": "1.22.0", "binary": "/usr/bin/go", "binary_sha256": "a" * 64}

            def build(command, _log_path, **_kwargs):
                target = Path(command[3]); target.parent.mkdir(parents=True, exist_ok=True); target.write_text("binary", encoding="utf-8")

            with mock.patch.object(benchmark, "REPO_ROOT", root), mock.patch.object(benchmark, "resolve_go", return_value=toolchain), mock.patch.object(
                benchmark, "run_tee", side_effect=build
            ) as runner, mock.patch.object(benchmark, "GO_TOOLCHAIN", None), mock.patch.object(benchmark, "BUILT_THIS_PROCESS", set()):
                built = benchmark.ensure_binaries(run_dir, ["queries"], False)
            self.assertEqual(runner.call_args.args[0][0], "/usr/bin/go")
            self.assertEqual(built[benchmark.BINARIES["queries"]]["go_toolchain"], toolchain)
            marker = json.loads((run_dir / "results" / f"built-{benchmark.BINARIES['queries']}").read_text(encoding="utf-8"))
            self.assertEqual(marker["go_toolchain"]["source"], "system")
            self.assertIn('"version": "1.22.0"', (run_dir / "logs" / "build.log").read_text(encoding="utf-8"))


class ManagedDatabaseTests(unittest.TestCase):
    def args(self, root: Path, mode: str | None = None) -> argparse.Namespace:
        values = ["load", "--database-id", "db-a", "--database-root", str(root), "--database", "benchmark"]
        if mode: values.extend(["--database-mode", mode])
        if mode == "reset": values.extend(["--confirm-reset", "benchmark"])
        return benchmark.make_parser().parse_args(values)

    def prepare_database(self, root: Path) -> Path:
        installation = root / "installation"; installation.mkdir(parents=True)
        binary = installation / "influxdb3"
        binary.write_text("#!/bin/sh\necho 'influxdb3 InfluxDB 3 Core, 3.11.1, revision test'\n", encoding="utf-8")
        binary.chmod(0o755)
        path = root / "db-a"; path.mkdir(); (path / "data").mkdir(); (path / "logs").mkdir()
        benchmark.save_json(path / "manifest.json", {
            "schema_version": 1, "kind": "influxdb3-database", "database_id": "db-a",
            "edition": "core", "version": "3.11.1", "installation_path": str(installation),
            "binary_sha256": benchmark.sha256_file(binary), "node_id": "db-a-node",
            "cluster_id": None, "license": {"status": "not-required", "source": None},
            "created_at": benchmark.utc_now(), "database": None, "binding": None,
        })
        return path

    def test_workspace_bind_reuse_reset_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.prepare_database(root); args = self.args(root); path, db = benchmark.prepare_database_workspace(args)
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
        args = benchmark.make_parser().parse_args(["query"])
        benchmark.resolve_database(args)
        with self.assertRaisesRegex(benchmark.BenchmarkError, "exactly one"):
            benchmark.validate_args(args)

    def test_failed_preflight_does_not_bind_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = self.prepare_database(root); args = self.args(root)
            with mock.patch.object(
                benchmark.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["influxdb3", "--version"], 15),
            ):
                with self.assertRaisesRegex(benchmark.BenchmarkError, "not runnable"):
                    benchmark.prepare_database_workspace(args)
            persisted = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertIsNone(persisted["database"])
            self.assertIsNone(persisted["binding"])


class TargetTests(unittest.TestCase):
    def test_sync_is_default_and_no_sync_is_opt_in(self) -> None:
        parser = benchmark.make_parser()
        durable = parser.parse_args(["load", "--database-id", "db-a"])
        non_durable = parser.parse_args([
            "load", "--database-id", "db-a", "--no-sync",
        ])
        self.assertFalse(durable.no_sync)
        self.assertEqual(durable.shutdown_timeout, 60)
        self.assertTrue(non_durable.no_sync)

    def test_managed_server_timeouts_must_be_positive(self) -> None:
        for option in ("--startup-timeout", "--shutdown-timeout"):
            args = benchmark.make_parser().parse_args([
                "load", "--database-id", "db-a", option, "0",
            ])
            benchmark.resolve_database(args)
            with self.assertRaisesRegex(benchmark.BenchmarkError, "must be positive"):
                benchmark.validate_args(args)

    def test_external_target_validation_and_token_redaction(self) -> None:
        args = benchmark.make_parser().parse_args([
            "all", "--url", "http://node-a:8181", "--url", "http://node-b:8181",
            "--edition", "enterprise", "--database-mode", "create",
        ])
        benchmark.resolve_database(args); benchmark.validate_args(args)
        with mock.patch.dict("os.environ", {
            "INFLUXDB3_AUTH_TOKEN": "query-secret",
            "INFLUXDB3_ADMIN_TOKEN": "admin-secret",
        }):
            command = benchmark.credential_args(args, include_admin=True)
        displayed = benchmark.display_command(command)
        self.assertNotIn("query-secret", displayed)
        self.assertNotIn("admin-secret", displayed)
        self.assertEqual(displayed.count("<redacted>"), 2)

    def test_influxdb3_binaries_and_query_format(self) -> None:
        self.assertEqual(benchmark.BINARIES["load"], "tsbs_load_influx3")
        self.assertEqual(benchmark.BINARIES["query"], "tsbs_run_queries_influx3")
        spec = benchmark.query_set_spec(
            {"dataset_id": "data", "spec": {"use_case": "cpu-only"}},
            {"seed": 1, "scale": 1, "start": "2023-01-01T00:00:00Z", "end": "2023-01-02T00:00:00Z", "query_counts": {"lastpoint": 1}},
        )
        self.assertEqual(spec["format"], "influx3")
        self.assertTrue(benchmark.query_set_id(spec).startswith("influx3-"))

    def test_external_nodes_must_report_matching_versions(self) -> None:
        with mock.patch.object(benchmark, "probe_server", side_effect=[
            {"version": "3.11.1", "revision": "a", "build": "enterprise"},
            {"version": "3.11.0", "revision": "b", "build": "enterprise"},
        ]):
            with self.assertRaisesRegex(benchmark.BenchmarkError, "different version"):
                benchmark.probe_servers(["http://a", "http://b"])


if __name__ == "__main__":
    unittest.main()
