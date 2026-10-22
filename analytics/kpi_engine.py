"""KPI engine for the Retail AI Core.

Implements the four internationally standard retail KPIs from the knowledge
base, computed entirely from local SQLite rows:

  * Footfall          – total visitors per hour/day
  * Peak Hours        – time slots with maximum traffic
  * Staffing Gap      – high crowd volume with no staff present
  * Congestion Index  – current people / store capacity

The engine is deliberately pure: it only needs a DBHandler, so it is trivial
to unit-test with an in-memory database.
"""

from __future__ import annotations

import math
from typing import Any, Optional

CONGESTION_BANDS = ("Normal", "Congested", "Critical")


def congestion_level(count: int, capacity: int, congested_at: float = 0.6,
                     critical_at: float = 0.9) -> str:
    """Map (people, capacity) to a KB status band."""
    if capacity <= 0:
        return "Normal"
    index = count / capacity
    if index >= critical_at:
        return "Critical"
    if index >= congested_at:
        return "Congested"
    return "Normal"


def congestion_index(count: int, capacity: int) -> float:
    """Occupancy ratio; 0.0 when capacity is unknown/zero."""
    if capacity <= 0:
        return 0.0
    return round(count / capacity, 3)


class KpiEngine:
    """Computes aggregated metrics from a DBHandler."""

    def __init__(self, db: Any, capacity: int = 50,
                 staffing_threshold: int = 15,
                 congested_at: float = 0.6,
                 critical_at: float = 0.9) -> None:
        self.db = db
        self.capacity = int(capacity)
        self.staffing_threshold = int(staffing_threshold)
        self.congested_at = float(congested_at)
        self.critical_at = float(critical_at)

    # ------------------------------------------------------------ single KPI
    def footfall(self, since: Optional[int] = None) -> int:
        """Total visitors counted (sum of per-frame counts)."""
        return sum(int(r["count"]) for r in self.db.fetch_rows(since=since))

    def peak_hour(self, top_n: int = 1) -> Optional[dict[str, Any]]:
        """The single busiest hour of day across history."""
        peak = self.db.get_peak_hours(top_n=top_n)
        return peak[0] if peak else None

    def staffing_gaps(self, since: Optional[int] = None) -> list[dict[str, Any]]:
        """Events where crowd > threshold AND no staff were present."""
        return [
            {
                "timestamp": int(r["timestamp"]),
                "camera_id": r["camera_id"],
                "count": int(r["count"]),
            }
            for r in self.db.fetch_rows(since=since)
            if int(r["count"]) > self.staffing_threshold
            and not int(r["staff_present"])
        ]

    def latest_congestion(self, count: int) -> dict[str, Any]:
        """Congestion snapshot for the most recent count."""
        return {
            "level": congestion_level(
                count, self.capacity, self.congested_at, self.critical_at
            ),
            "index": congestion_index(count, self.capacity),
        }

    # ------------------------------------------------------------ zone ops
    def zone_breakdown(self, since: Optional[int] = None) -> dict[str, Any]:
        """Per-camera (zone) operational metrics over the window.

        Cafe owners read this as: which zone (counter / entrance / seating)
        is busiest right now and today.
        """
        import time

        since = int(since) if since is not None else int(time.time()) - 86400
        zones: dict[str, dict[str, Any]] = {}
        for r in self.db.fetch_rows(since=since):
            cam = r["camera_id"]
            z = zones.setdefault(cam, {
                "footfall": 0, "observations": 0, "last_count": 0, "last_seen": 0,
            })
            z["footfall"] += int(r["count"])
            z["observations"] += 1
            if int(r["timestamp"]) >= int(z["last_seen"]):
                z["last_seen"] = int(r["timestamp"])
                z["last_count"] = int(r["count"])
        return zones

    def staffing_plan(self, days: int = 7) -> list[dict[str, Any]]:
        """Recommended staff per hour of day, from the last `days` of traffic.

        Rule: recommended = ceil(avg footfall in that hour / staffing
        threshold), 0 when the hour has no data. This turns the KB's
        "Staffing Gap" KPI into an actionable daily schedule for cafe owners.
        """
        import time

        since = int(time.time()) - int(days) * 86400
        sums: dict[int, int] = {}
        counts: dict[int, int] = {}
        for r in self.db.fetch_rows(since=since):
            h = (int(r["timestamp"]) % 86400) // 3600
            sums[h] = sums.get(h, 0) + int(r["count"])
            counts[h] = counts.get(h, 0) + 1
        plan = []
        for h in range(24):
            n = counts.get(h, 0)
            avg = sums.get(h, 0) / n if n else 0.0
            recommended = 0
            if avg > 0:
                recommended = max(1, math.ceil(avg / max(1, self.staffing_threshold)))
            plan.append({
                "hour": h,
                "avg_footfall": round(avg, 1),
                "recommended_staff": recommended,
            })
        return plan

    # ------------------------------------------------------------- summaries
    def calculate_hourly_metrics(self, now: Optional[int] = None) -> dict[str, Any]:
        """JSON-serializable aggregate used for the dashboard report.

        Returns the KB's `last_analytics_report` payload shape, extended with
        the fields the dashboard charts consume.
        """
        import time

        now = int(now) if now is not None else int(time.time())
        today_start = now - (now % 86400)
        yesterday_start = today_start - 86400

        hourly = self.db.get_hourly_counts(since=today_start)
        daily = self.db.get_daily_counts()
        peak = self.peak_hour()
        gaps = self.staffing_gaps(since=yesterday_start)
        footfall_today = sum(h["footfall"] for h in hourly)
        footfall_yesterday = daily[-2]["footfall"] if len(daily) >= 2 else 0

        latest = self.db.fetch_rows()[-1] if self.db.fetch_rows() else None
        current = int(latest["count"]) if latest is not None else 0
        current_congestion = self.latest_congestion(current)

        # Operational efficiency: share of busy hours where staff was present.
        staffed_busy = staffed_hours = 0
        for r in self.db.fetch_rows(since=today_start):
            if int(r["count"]) > 0:
                staffed_hours += 1
                if int(r["staff_present"]):
                    staffed_busy += 1
        efficiency = (
            round(100.0 * staffed_busy / staffed_hours, 1) if staffed_hours else 0.0
        )

        trend_pct = None
        if footfall_yesterday > 0:
            trend_pct = round(
                100.0 * (footfall_today - footfall_yesterday) / footfall_yesterday, 1
            )

        return {
            "generated_at": now,
            "peak_hour": f"{peak['hour']:02d}:00" if peak else None,
            "peak_hour_distribution": hourly,
            "total_visits": footfall_today,
            "footfall_today": footfall_today,
            "footfall_yesterday": footfall_yesterday,
            "trend_pct_vs_yesterday": trend_pct,
            "staffing_gaps": len(gaps),
            "last_staffing_gaps": gaps[-5:],
            "congestion_index": current_congestion["index"],
            "congestion_level": current_congestion["level"],
            "efficiency": f"{efficiency}%",
            "current_count": current,
            # Operational extras for cafe owners:
            "zones": self.zone_breakdown(since=today_start),
            "staffing_plan": self.staffing_plan(days=7),
        }
