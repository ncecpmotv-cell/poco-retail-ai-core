"""Tests for the SQLite BI hub."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.db_handler import DBHandler  # noqa: E402


class DBHandlerTests(unittest.TestCase):
    def setUp(self):
        self.db = DBHandler(":memory:")
        self.now = int(time.time())

    def tearDown(self):
        self.db.close()

    def _seed(self):
        hour = self.now - (self.now % 3600)
        self.db.save_telemetry(
            "cam1", "three people at entrance", 3, "low", True,
            timestamp=hour,
        )
        self.db.save_telemetry(
            "cam1", "ten people waiting", 10, "medium", False,
            timestamp=hour + 60,
        )
        self.db.save_telemetry(
            "cam2", "empty aisle", 0, "low", False,
            timestamp=hour + 120,
        )

    def test_save_and_fetch_roundtrip(self):
        row_id = self.db.save_telemetry(
            "cam1", "two people", 2, "low", True, "/tmp/x.jpg", timestamp=1000
        )
        self.assertGreater(row_id, 0)
        rows = self.db.fetch_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["camera_id"], "cam1")
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["staff_present"], 1)
        self.assertEqual(rows[0]["snapshot_path"], "/tmp/x.jpg")

    def test_hourly_counts(self):
        self._seed()
        buckets = self.db.get_hourly_counts()
        # seed rows at hour, +60s, +120s → all inside ONE hour bucket
        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["footfall"], 13)

    def test_peak_hours(self):
        self._seed()
        peak = self.db.get_peak_hours(top_n=1)
        self.assertEqual(len(peak), 1)
        self.assertEqual(peak[0]["footfall"], 13)

    def test_prune(self):
        self._seed()
        old = self.now - 200 * 86400
        self.db.save_telemetry("cam9", "ancient", 1, "low", False, timestamp=old)
        removed = self.db.prune_older_than(days=90)
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.db.fetch_rows()), 3)

    def test_persists_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.db")
            db = DBHandler(path)
            db.save_telemetry("cam1", "x", 1, "low", False, timestamp=5)
            db.close()
            db2 = DBHandler(path)
            self.assertEqual(len(db2.fetch_rows()), 1)
            db2.close()


if __name__ == "__main__":
    unittest.main()
