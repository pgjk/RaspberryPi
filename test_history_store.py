import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from history_store import HistoryStore


class HistoryStoreTests(unittest.TestCase):
    def test_samples_are_restored_after_reopening_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "history.sqlite3"
            store = HistoryStore(database, ["house", "outside"], 10, 90)
            timestamp = "2026-10-27T10:00:00"
            store.append(
                timestamp,
                {"house": 20.5, "outside": 4.0},
                valve_voltage=3.25,
            )
            store.close()

            reopened = HistoryStore(database, ["house", "outside"], 10, 90)
            history = reopened.load_latest()
            reopened.close()

        self.assertEqual(history["house"], [{"time": timestamp, "value": 20.5}])
        self.assertEqual(history["outside"], [{"time": timestamp, "value": 4.0}])
        self.assertEqual(
            history["valve_voltage"], [{"time": timestamp, "value": 3.25}]
        )

    def test_load_returns_only_latest_configured_points_in_time_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(Path(directory) / "history.sqlite3", ["house"], 2, 90)
            for index in range(3):
                store.append(
                    f"2026-10-27T10:00:0{index}",
                    {"house": float(index)},
                    valve_voltage=float(index),
                )

            history = store.load_latest()
            store.close()

        self.assertEqual(
            [entry["value"] for entry in history["house"]],
            [1.0, 2.0],
        )

    def test_old_samples_are_removed_by_retention_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "history.sqlite3"
            store = HistoryStore(database, ["house"], 10, 1)
            old_timestamp = (
                datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
            ).isoformat()
            store.append(old_timestamp, {"house": 19.0}, valve_voltage=0.0)
            store.close()

            reopened = HistoryStore(database, ["house"], 10, 1)
            history = reopened.load_latest()
            reopened.close()

        self.assertEqual(history["house"], [])

    def test_store_operations_are_safe_across_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(
                Path(directory) / "history.sqlite3", ["house"], 10, 90
            )
            failures = []

            def write_sample():
                try:
                    store.append(
                        "2026-10-27T10:00:00",
                        {"house": 20.0},
                        valve_voltage=2.0,
                    )
                except Exception as exc:
                    failures.append(exc)

            worker = threading.Thread(target=write_sample)
            worker.start()
            worker.join()
            history = store.load_latest()
            store.close()

        self.assertEqual(failures, [])
        self.assertEqual(len(history["house"]), 1)


if __name__ == "__main__":
    unittest.main()
