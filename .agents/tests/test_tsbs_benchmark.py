from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

import tsbs_benchmark as shared  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def test_atomic_json_round_trip_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "manifest.json"
            shared.save_json(path, {"value": 1})

            self.assertEqual(shared.read_json(path, RuntimeError), {"value": 1})
            self.assertEqual(len(shared.sha256_file(path)), 64)
            self.assertFalse(any(path.parent.glob("*.tmp-*")))

    def test_new_run_dir_does_not_reuse_an_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = shared.new_run_dir(root)
            first.mkdir()
            second = shared.new_run_dir(root)

            self.assertNotEqual(first, second)
            self.assertEqual(second.parent, root)


class WorkloadTests(unittest.TestCase):
    def args(self, **overrides: object) -> argparse.Namespace:
        values = {
            "profile": "smoke",
            "start": None,
            "end": None,
            "scale": None,
            "seed": None,
            "log_interval": None,
            "load_workers": None,
            "query_workers": None,
            "batch_size": None,
            "queries": None,
            "query_type": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_profile_is_copied_and_query_selection_is_canonical(self) -> None:
        workload = shared.build_workload(
            self.args(query_type=["lastpoint", "cpu-max-all-1"], queries=3)
        )

        self.assertEqual(
            workload["query_counts"], {"cpu-max-all-1": 3, "lastpoint": 3}
        )
        workload["query_counts"]["lastpoint"] = 99
        self.assertEqual(shared.PROFILES["smoke"]["query_counts"]["lastpoint"], 10)

    def test_existing_workload_is_reused_when_profile_is_omitted(self) -> None:
        base = json.loads(json.dumps(shared.PROFILES["manual"]))
        workload = shared.build_workload(self.args(profile=None, scale=7), base)

        self.assertEqual(workload["scale"], 7)
        self.assertEqual(workload["start"], base["start"])


class ResultTests(unittest.TestCase):
    def write_json(self, root: Path, relative: str, value: dict) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_structured_results_are_aggregated_with_weighted_latency(self) -> None:
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
            for attempt, count, mean in ((1, 10, 2.0), (2, 30, 4.0)):
                self.write_json(
                    run_dir,
                    f"results/query-{attempt}.json",
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
            manifest = {
                "events": {
                    "loads": [
                        {
                            "attempt": 1,
                            "database": "benchmark",
                            "database_mode": "create",
                            "status": "completed",
                            "log": "logs/load.log",
                            "results": "results/load.json",
                        }
                    ],
                    "queries": [
                        {
                            "query_type": "lastpoint",
                            "attempt": attempt,
                            "database": "benchmark",
                            "status": "completed",
                            "log": f"logs/query-{attempt}.log",
                            "results": f"results/query-{attempt}.json",
                        }
                        for attempt in (1, 2)
                    ],
                }
            }

            summary = shared.build_summary(run_dir, manifest)

        self.assertEqual(summary["failures"], [])
        self.assertEqual(summary["ingestion_runs"][0]["metrics"], 200)
        self.assertEqual(summary["queries"][0]["query_count"], 40)
        self.assertEqual(summary["queries"][0]["weighted_mean_milliseconds"], 3.5)

    def test_reused_load_does_not_require_a_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            summary = shared.build_summary(
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
            summary = shared.build_summary(
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

    def test_invalid_current_result_is_reported_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            self.write_json(
                run_dir,
                "results/query.json",
                {"ResultFormatVersion": "0.2", "Totals": {"overallStats": {}}},
            )
            event = {
                "query_type": "lastpoint",
                "attempt": 1,
                "database": "benchmark",
                "status": "completed",
                "log": "logs/query.log",
                "results": "results/query.json",
            }
            incomplete = shared.build_summary(
                run_dir, {"events": {"loads": [], "queries": [event]}}
            )

            (run_dir / "results/query.json").write_text(
                "{not-json", encoding="utf-8"
            )
            malformed = shared.build_summary(
                run_dir, {"events": {"loads": [], "queries": [event]}}
            )

        self.assertEqual(incomplete["queries"], [])
        self.assertIn("all_queries", incomplete["failures"][0]["reason"])
        self.assertIn("malformed result JSON", malformed["failures"][0]["reason"])

    def test_load_parsers_preserve_optional_row_metrics(self) -> None:
        structured = shared.parse_load_result(
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
        legacy = shared.parse_load_log(
            "loaded 200 metrics in 2.000sec (mean rate 100.00 metrics/sec)\n"
            "loaded 40 rows in 2.000sec (mean rate 20.00 rows/sec)\n"
        )

        self.assertNotIn("rows", structured)
        self.assertEqual(legacy["rows_per_second"], 20.0)


if __name__ == "__main__":
    unittest.main()
