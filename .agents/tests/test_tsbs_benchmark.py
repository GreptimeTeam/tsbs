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

    def test_legacy_query_log_remains_supported(self) -> None:
        parsed = shared.parse_query_log(
            "Run complete after 10 queries\nall queries:\n"
            "min: 1ms, mean: 3.50ms, max: 4ms, count: 10\n"
        )

        self.assertEqual(parsed, {"mean_milliseconds": 3.5, "count": 10})


if __name__ == "__main__":
    unittest.main()
