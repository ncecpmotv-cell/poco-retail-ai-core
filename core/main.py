"""Retail AI Core daemon — the 60-second edge loop.

Loop (KB Task 4):
    sync config (getUpdates) -> subscription firewall -> process cameras ->
    save telemetry (SQLite) -> hourly KPI -> post state envelope (channel).

Design rules: single-threaded, one camera failure never kills the loop,
graceful shutdown on SIGTERM/SIGINT, local-first (offline tolerant).
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

# When run directly (`python core/main.py`), Python puts core/ on sys.path
# instead of the repo root — make the sibling packages importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics.db_handler import DBHandler
from analytics.kpi_engine import KpiEngine, congestion_level
from core.cloud_sync import CloudSync, CloudSyncError
from core.config import Config
from core.inference_engine import build_prompt, run_inference
from core.stream_manager import snapshot_all

log = logging.getLogger("retail_ai.main")
_SHUTDOWN = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _SHUTDOWN
    log.info("signal %s received; shutting down gracefully", signum)
    _SHUTDOWN = True


def setup_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


class RetailAIDaemon:
    """Owns the loop state: config, DB, KPI engine, cloud bridge."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = DBHandler(cfg.db_path)
        self.kpi = KpiEngine(
            self.db,
            capacity=cfg.store_capacity,
            staffing_threshold=cfg.staffing_threshold,
            congested_at=cfg.congested_at,
            critical_at=cfg.critical_at,
        )
        self.cloud: Optional[CloudSync] = None
        if cfg.bot_token:
            self.cloud = CloudSync(
                bot_token=cfg.bot_token,
                state_channel_id=cfg.state_channel_id,
                admin_id=cfg.admin_id,
                default_expiry=cfg.default_subscription_expiry,
                bridge_secret=cfg.bridge_secret,
            )
        else:
            log.warning("BOT_TOKEN is empty — cloud sync disabled (local-only mode)")
        self.cameras: list[dict[str, Any]] = list(cfg.local_cameras)
        self.ai_prompt: str = cfg.local_ai_prompt
        self._last_state_posted: int = 0
        # Device metrics + alert state
        self._started_mono: float = time.monotonic()
        self.last_inference_ms: Optional[int] = None
        self.inference_ok: int = 0
        self.inference_fail: int = 0
        self._last_alert_sent: int = 0
        self._last_alert_text: str = ""
        self._last_gap_count: int = 0
        # Per-camera health (cafe owners watch for flaky cameras)
        self.cam_health: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ steps
    def sync_config(self) -> None:
        """Pull dashboard envelopes (blocking long-poll, bounded)."""
        if self.cloud is None:
            return
        try:
            self.cloud.poll_incoming()
            # CloudSync.apply_envelope is the single source of truth.
            if getattr(self.cloud, "cameras", None):
                self.cameras = self.cloud.cameras
            if getattr(self.cloud, "ai_target_prompt", ""):
                self.ai_prompt = self.cloud.ai_target_prompt
            # Live thresholds pushed by the dashboard — update the KPI engine
            # so congestion + staffing math matches the owner's settings.
            cap = getattr(self.cloud, "store_capacity", None)
            thr = getattr(self.cloud, "staffing_threshold", None)
            if cap is not None:
                self.kpi.capacity = int(cap)
                self.cfg.store_capacity = int(cap)
            if thr is not None:
                self.kpi.staffing_threshold = int(thr)
                self.cfg.staffing_threshold = int(thr)
        except CloudSyncError as exc:
            log.warning("sync failed (offline?): %s", exc)

    def _record_health(self, camera_id: str, ok: bool) -> None:
        h = self.cam_health.setdefault(camera_id, {"ok": 0, "fail": 0})
        h["ok" if ok else "fail"] += 1

    def process_cameras(self) -> dict[str, Optional[str]]:
        """Grab + infer for every camera; returns {cam_id: snapshot_path}."""
        snapshots = snapshot_all(self.cameras, self.cfg.snapshot_dir)
        for cam in self.cameras:
            cam_id = str(cam.get("id", "unknown"))
            snapshot = snapshots.get(cam_id)
            if not snapshot:
                self._record_health(cam_id, False)
                continue
            role = str(cam.get("role", "general"))
            prompt = build_prompt(role, self.ai_prompt)
            t0 = time.monotonic()
            result = run_inference(
                self.cfg.minicpmv_cli_path,
                self.cfg.model_dir,
                self.cfg.model_file,
                snapshot,
                prompt,
                timeout_seconds=self.cfg.inference_timeout_seconds,
                mmproj_file=self.cfg.mmproj_file,
            )
            self.last_inference_ms = int((time.monotonic() - t0) * 1000)
            if result is None:
                self.inference_fail += 1
                self._record_health(cam_id, False)
                log.warning("camera %s: inference failed", cam_id)
                continue
            self.inference_ok += 1
            self._record_health(cam_id, True)
            self.db.save_telemetry(
                camera_id=cam_id,
                raw_text=result["raw_text"],
                count=result["count"],
                congestion_level=result["congestion_level"],
                staff_present=result["staff_present"],
                snapshot_path=snapshot,
            )
            log.info(
                "camera %s: count=%d staff=%s congestion=%s",
                cam_id, result["count"], result["staff_present"],
                result["congestion_level"],
            )
        return snapshots

    def publish_state(self, force: bool = False) -> None:
        """Compute the current status + hourly report and pin them."""
        if self.cloud is None:
            return
        now = int(time.time())
        if not force and now - self._last_state_posted \
                < self.cfg.state_post_interval_seconds:
            return
        report = self.kpi.calculate_hourly_metrics(now=now)
        level = report["congestion_level"]
        if not self.cloud.is_subscription_active():
            status = "Blocked"
            alert = "Subscription expired — contact your admin."
        else:
            status = level
            alert = (
                f"Staffing gap: {report['staffing_gaps']} events in the last 24h"
                if report["staffing_gaps"] else None
            )
        current_status = {
            "status": status,
            "count": report["current_count"],
            "alert": alert,
            "camera_id": None,
            "congestion_index": report["congestion_index"],
            "timestamp": now,
        }
        device = {
            "uptime_seconds": int(time.monotonic() - self._started_mono),
            "last_inference_ms": self.last_inference_ms,
            "inference_ok": self.inference_ok,
            "inference_fail": self.inference_fail,
            "cameras_health": self.cam_health,
        }
        envelope = self.cloud.build_state_envelope(current_status, report, device)
        try:
            self.cloud.post_state(envelope)
            self._last_state_posted = now
            log.info("state posted: %s (count=%d)", status, report["current_count"])
        except CloudSyncError as exc:
            log.warning("state post failed (offline?): %s", exc)
        self.maybe_alert(status, report, now)

    # ---------------------------------------------------------------- alerts
    def maybe_alert(self, status: str, report: dict[str, Any], now: int) -> None:
        """Send a chat alert on Critical status or new staffing gaps
        (rate-limited by ALERT_COOLDOWN_SECONDS)."""
        if not self.cfg.alert_enabled or self.cloud is None:
            return
        chat_id = self.cfg.alert_chat_id or self.cfg.user_id
        if not chat_id:
            return
        new_gaps = report["staffing_gaps"] > self._last_gap_count
        text = None
        # Off-hours security check (cafe closed but people detected).
        if self.cfg.open_hours_start != self.cfg.open_hours_end \
                and report["current_count"] > 0:
            hour = time.localtime().tm_hour
            if self.cfg.open_hours_start < self.cfg.open_hours_end:
                in_hours = self.cfg.open_hours_start <= hour < self.cfg.open_hours_end
            else:  # overnight schedule, e.g. 18:00 -> 02:00
                in_hours = hour >= self.cfg.open_hours_start or hour < self.cfg.open_hours_end
            if not in_hours:
                text = (
                    f"🔦 Retail AI — unexpected activity OUTSIDE opening hours: "
                    f"{report['current_count']} people detected."
                )
        if text is None and status == "Critical":
            text = (
                f"🚨 Retail AI — CRITICAL congestion: {report['current_count']} "
                f"people in store (index {report['congestion_index']})."
            )
        if text is None:
            base = report.get("today_vs_baseline") or {}
            if base.get("anomaly") and (base.get("pct_diff") or 0) < 0:
                text = (
                    f"📉 Retail AI — traffic is {base['pct_diff']}% below a "
                    f"typical {base.get('weekday', 'day')} so far. "
                    "Worth checking (closed early? camera issue?)."
                )
        elif new_gaps and report["staffing_gaps"] > 0:
            text = (
                f"⚠️ Retail AI — staffing gap detected: {report['staffing_gaps']} "
                f"crowded events without staff in the last 24h."
            )
        if text is None:
            self._last_gap_count = max(self._last_gap_count, report["staffing_gaps"])
            return
        if text == self._last_alert_text and now - self._last_alert_sent \
                < self.cfg.alert_cooldown_seconds:
            return
        try:
            self.cloud.send_message(chat_id, text)
            self._last_alert_sent = now
            self._last_alert_text = text
            log.info("alert sent: %s", text)
        except CloudSyncError as exc:
            log.warning("alert send failed: %s", exc)
        self._last_gap_count = report["staffing_gaps"]

    # -------------------------------------------------------- weekly report
    def maybe_weekly_report(self) -> None:
        """Send the Monday morning summary once per week (state persisted)."""
        import json

        if not self.cfg.weekly_report_enabled or self.cloud is None:
            return
        chat_id = self.cfg.alert_chat_id or self.cfg.user_id
        if not chat_id:
            return
        lt = time.localtime()
        if lt.tm_wday != 0 or lt.tm_hour < self.cfg.weekly_report_hour:
            return
        key = f"{lt.tm_year}-{lt.tm_yday}"
        state_file = "logs/weekly_state.json"
        try:
            with open(state_file) as fh:
                sent = json.load(fh)
        except (OSError, ValueError):
            sent = None
        if sent == key:
            return
        summary = self.kpi.weekly_summary(days=7)
        dwell = (summary.get("dwell") or {}).get("dwell_minutes")
        base = summary.get("today_vs_baseline") or {}
        text = (
            "📊 Weekly Retail AI report\n"
            f"• total footfall: {summary['total_footfall']} "
            f"(avg {summary['avg_daily']}/day over {summary['days_with_data']} days)\n"
            f"• busiest day: {summary['busiest_day'] or '—'}\n"
            f"• peak hour: {summary['peak_hour'] or '—'}\n"
            f"• staffing gaps: {summary['staffing_gaps']}\n"
            f"• avg dwell: {dwell if dwell is not None else '—'} min\n"
            f"• today vs typical {base.get('weekday', 'day')}: "
            f"{base.get('pct_diff', '—')}%"
        )
        try:
            self.cloud.send_message(chat_id, text)
            with open(state_file, "w") as fh:
                json.dump(key, fh)
            log.info("weekly report sent")
        except (CloudSyncError, OSError) as exc:
            log.warning("weekly report failed: %s", exc)

    # -------------------------------------------------------------- commands
    def handle_commands(self) -> None:
        """Answer /status, /cameras, /ping from the owner/admin chat."""
        if self.cloud is None:
            return
        for cmd in self.cloud.pending_commands:
            sender = int(cmd.get("from_id") or 0)
            if self.cfg.admin_id and sender not in (self.cfg.admin_id, self.cfg.user_id):
                continue
            chat_id = cmd.get("chat_id")
            text = cmd["text"].lower()
            reply = None
            if text.startswith("/ping"):
                reply = "pong 🟢"
            elif text.startswith("/status"):
                report = self.kpi.calculate_hourly_metrics()
                base = report.get("today_vs_baseline") or {}
                dwell = (report.get("dwell") or {}).get("dwell_minutes")
                reply = (
                    "📊 Retail AI status\n"
                    f"• people now: {report['current_count']}\n"
                    f"• congestion: {report['congestion_level']} "
                    f"({report['congestion_index']})\n"
                    f"• footfall today: {report['footfall_today']}\n"
                    f"• staffing gaps (24h): {report['staffing_gaps']}\n"
                    f"• today vs typical {base.get('weekday', 'day')}: "
                    f"{base.get('pct_diff', '—')}%\n"
                    f"• avg dwell (3h): {dwell if dwell is not None else '—'} min\n"
                    f"• uptime: {int(time.monotonic() - self._started_mono) // 60} min\n"
                    f"• last inference: {self.last_inference_ms or '—'} ms"
                )
            elif text.startswith("/cameras"):
                lines = []
                for c in self.cameras:
                    h = self.cam_health.get(str(c.get("id")), {"ok": 0, "fail": 0})
                    status_icon = "🟢" if h["fail"] == 0 else ("🟡" if h["ok"] > h["fail"] else "🔴")
                    lines.append(
                        f"{status_icon} {c.get('id')} ({c.get('role', 'general')}) "
                        f"ok={h['ok']} fail={h['fail']} — {c.get('url')}"
                    )
                reply = "📹 Cameras:\n" + ("\n".join(lines) if lines else "  none configured")
            elif text.startswith("/export"):
                parts = text.split()
                days = 1
                if len(parts) > 1 and parts[1].isdigit():
                    days = min(max(int(parts[1]), 1), 30)
                path = self.export_csv(days)
                if path:
                    try:
                        self.cloud.send_document(
                            chat_id, path, caption=f"📊 Telemetry export — last {days} day(s)"
                        )
                        reply = None  # document already sent
                    except CloudSyncError as exc:
                        reply = f"Export upload failed: {exc}"
                else:
                    reply = "No telemetry in that window yet."
            if reply:
                try:
                    self.cloud.send_message(chat_id, reply)
                except CloudSyncError as exc:
                    log.warning("command reply failed: %s", exc)
        self.cloud.pending_commands.clear()

    def export_csv(self, days: int = 1) -> Optional[str]:
        """Write the last N days of telemetry to a CSV file; return its path."""
        import csv

        since = int(time.time()) - days * 86400
        rows = self.db.fetch_rows(since=since)
        if not rows:
            return None
        path = f"logs/export_{int(time.time())}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "timestamp", "camera_id", "count",
                             "congestion_level", "staff_present", "raw_text"])
            for r in rows:
                writer.writerow([
                    r["id"], r["timestamp"], r["camera_id"], r["count"],
                    r["congestion_level"], r["staff_present"],
                    (r["raw_text"] or "")[:300],
                ])
        log.info("exported %d rows to %s", len(rows), path)
        return path

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)
        log.info(
            "Retail AI daemon started (interval=%ds, cameras=%d)",
            self.cfg.loop_interval_seconds, len(self.cameras),
        )
        while not _SHUTDOWN:
            started = time.monotonic()
            self.sync_config()
            self.handle_commands()
            if self.cloud is None or self.cloud.is_subscription_active():
                try:
                    self.process_cameras()
                except Exception as exc:  # noqa: BLE001 — never kill the loop
                    log.error("camera processing error: %s", exc)
            else:
                log.info("subscription expired; inference paused")
            self.publish_state()
            self.maybe_weekly_report()
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.cfg.loop_interval_seconds - elapsed))
        self.db.close()
        log.info("daemon stopped cleanly")


def main() -> None:
    cfg = Config.load()
    setup_logging(cfg.log_file)
    RetailAIDaemon(cfg).run_forever()


if __name__ == "__main__":
    main()
