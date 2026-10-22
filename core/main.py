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
        except CloudSyncError as exc:
            log.warning("sync failed (offline?): %s", exc)

    def process_cameras(self) -> dict[str, Optional[str]]:
        """Grab + infer for every camera; returns {cam_id: snapshot_path}."""
        snapshots = snapshot_all(self.cameras, self.cfg.snapshot_dir)
        for cam in self.cameras:
            cam_id = str(cam.get("id", "unknown"))
            snapshot = snapshots.get(cam_id)
            if not snapshot:
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
                log.warning("camera %s: inference failed", cam_id)
                continue
            self.inference_ok += 1
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
        if status == "Critical":
            text = (
                f"🚨 Retail AI — CRITICAL congestion: {report['current_count']} "
                f"people in store (index {report['congestion_index']})."
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
                reply = (
                    "📊 Retail AI status\n"
                    f"• people now: {report['current_count']}\n"
                    f"• congestion: {report['congestion_level']} "
                    f"({report['congestion_index']})\n"
                    f"• footfall today: {report['footfall_today']}\n"
                    f"• staffing gaps (24h): {report['staffing_gaps']}\n"
                    f"• uptime: {int(time.monotonic() - self._started_mono) // 60} min\n"
                    f"• last inference: {self.last_inference_ms or '—'} ms"
                )
            elif text.startswith("/cameras"):
                cams = "\n".join(
                    f"  {c.get('id')} ({c.get('role', 'general')}) — {c.get('url')}"
                    for c in self.cameras
                ) or "  none configured"
                reply = "📹 Cameras:\n" + cams
            if reply:
                try:
                    self.cloud.send_message(chat_id, reply)
                except CloudSyncError as exc:
                    log.warning("command reply failed: %s", exc)
        self.cloud.pending_commands.clear()

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
