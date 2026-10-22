"""Tests for the KPI engine (footfall, peak hour, staffing gap, congestion)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics.db_handler import DBHandler  # noqa: E402
from analytics.kpi_engine import (  # noqa: E402
    KpiEngine,
    congestion_index,
    congestion_level,
)

HOUR = 3600


class CongestionTests(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(congestion_level(5, 100), "Normal")
        self.assertEqual(congestion_level(60, 100), "Congested")
        self.assertEqual(congestion_level(90, 100), "Critical")

    def test_index(self):
        self.assertEqual(congestion_index(25, 100), 0.25)
        self.assertEqual(congestion_index(10, 0), 0.0)


class KpiEngineTests(unittest.TestCase):
    def setUp(self):
        self.db = DBHandler(":memory:")
        self.engine = KpiEngine(self.db, capacity=20, staffing_threshold=5)
        self.now = 1_750_000_000  # fixed epoch for deterministic tests

    def tearDown(self):
        self.db.close()

    def _seed(self):
        today_start = self.now - (self.now % 86400)
        # busy hour WITH staff
        for i in range(3):
            self.db.save_telemetry(
                "cam1", "queue", 8, "high", True,
                timestamp=today_start + 16 * HOUR + i * 60,
            )
        # busy hour WITHOUT staff → staffing gap
        self.db.save_telemetry(
            "cam1", "crowded, no staff", 9, "high", False,
            timestamp=today_start + 17 * HOUR,
        )
        # quiet frame
        self.db.save_telemetry(
            "cam2", "quiet", 1, "low", True,
            timestamp=today_start + 10 * HOUR,
        )
        # yesterday's traffic for the trend calc
        self.db.save_telemetry(
            "cam1", "yesterday", 4, "medium", True,
            timestamp=today_start - HOUR,
        )

    def test_footfall(self):
        self._seed()
        self.assertEqual(self.engine.footfall(), 8 * 3 + 9 + 1 + 4)

    def test_peak_hour(self):
        self._seed()
        peak = self.engine.peak_hour()
        self.assertEqual(peak["hour"], 16)  # 3×8=24 beats 9 and 4

    def test_staffing_gap_detection(self):
        self._seed()
        gaps = self.engine.staffing_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["count"], 9)
        self.assertFalse(
            any(g["count"] == 8 for g in gaps), "staffed busy frames are not gaps"
        )

    def test_hourly_metrics_shape(self):
        self._seed()
        report = self.engine.calculate_hourly_metrics(now=self.now)
        self.assertEqual(report["peak_hour"], "16:00")
        self.assertEqual(report["footfall_today"], 34)
        self.assertEqual(report["footfall_yesterday"], 4)
        self.assertEqual(report["staffing_gaps"], 1)
        self.assertIn("efficiency", report)
        self.assertGreaterEqual(report["congestion_index"], 0.0)
        # current_count = latest row (yesterday, 4) → 4/20 = 0.2 → Normal
        self.assertEqual(report["congestion_level"], "Normal")

    def test_empty_database(self):
        report = self.engine.calculate_hourly_metrics(now=self.now)
        self.assertIsNone(report["peak_hour"])
        self.assertEqual(report["footfall_today"], 0)
        self.assertEqual(report["efficiency"], "0.0%")
        self.assertEqual(report["congestion_level"], "Normal")


if __name__ == "__main__":
    unittest.main()
