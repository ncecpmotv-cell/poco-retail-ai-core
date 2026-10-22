"""Telegram channel-bus integration (zero-cloud-cost sync).

DESIGN NOTE (verified against the official Bot API docs): the knowledge base
proposes Bot API methods `getCloudStorageValues` / `setCloudStorageValues`.
Those methods DO NOT EXIST — Mini App CloudStorage is client-side only and a
Python daemon cannot read or write it. The working zero-cost equivalent is a
private Telegram channel used as a message bus, where the bot is an admin:

  Core -> Dashboard : sendMessage(state envelope) + pinChatMessage;
                      dashboard reads it via getChat -> pinned_message.text
  Dashboard -> Core : dashboard posts set_config / set_subscription envelopes
                      to the channel with a RELAY bot token; core receives
                      them as channel_post updates via getUpdates
                      (sendData/web_app_data is also supported as a fallback).

  Relay bot requirement (verified live): Telegram never delivers a bot's own
  channel posts back to that bot via getUpdates, so envelopes must be posted
  by a second bot (or sent via sendData) to reach the polling daemon. The
  main token therefore stays on the device only.

All envelopes are JSON objects. The KB's CloudStorage key names are kept as
envelope field names so the dashboard's CloudStorage mirror stays compatible.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests

log = logging.getLogger("retail_ai.cloud")

API_BASE = "https://api.telegram.org/bot{token}/{method}"
GET_UPDATES_TIMEOUT = 25  # Bot API long-poll seconds


class CloudSyncError(RuntimeError):
    """Raised when the Telegram bridge cannot be reached."""


class CloudSync:
    """Envelope bus + subscription firewall for the edge daemon."""

    def __init__(
        self,
        bot_token: str,
        state_channel_id: str,
        admin_id: int = 0,
        default_expiry: int = 4102444800,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not bot_token:
            raise ValueError("BOT_TOKEN is required for the Telegram bridge")
        self.bot_token = bot_token
        self.channel_id = state_channel_id
        self.admin_id = admin_id
        self.default_expiry = default_expiry
        self._session = session or requests.Session()
        self._offset: int = 0
        self.subscription_expiry: int = default_expiry
        self.last_state_payload: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------ low level
    def api_call(
        self, method: str, params: dict[str, Any], retries: int = 3
    ) -> Any:
        """POST to the Bot API with retry + exponential backoff."""
        url = API_BASE.format(token=self.bot_token, method=method)
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                resp = self._session.post(
                    url, json=params, timeout=30
                )
                resp.raise_for_status()
                body = resp.json()
                if not body.get("ok"):
                    raise CloudSyncError(
                        f"{method} failed: {body.get('description', 'unknown')}"
                    )
                return body.get("result")
            except (requests.RequestException, CloudSyncError, ValueError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise CloudSyncError(f"Telegram API unreachable: {last_exc}")

    # --------------------------------------------------------- core -> cloud
    def build_state_envelope(
        self, current_status: dict[str, Any], analytics_report: dict[str, Any]
    ) -> dict[str, Any]:
        """The pinned 'state' envelope the dashboard reads via getChat."""
        return {
            "type": "state",
            "updated_at": int(time.time()),
            "subscription_expiry": int(self.subscription_expiry),
            "current_status": current_status,
            "last_analytics_report": analytics_report,
        }

    def post_state(self, envelope: dict[str, Any]) -> Optional[int]:
        """Send the state envelope to the channel and pin it.

        Returns the message id, or None when the bridge is unconfigured.
        """
        if not self.channel_id:
            log.info("STATE_CHANNEL_ID unset; skipping state post")
            return None
        msg = self.api_call(
            "sendMessage",
            {
                "chat_id": self.channel_id,
                "text": json.dumps(envelope, ensure_ascii=False),
                "disable_notification": True,
            },
        )
        message_id = msg.get("message_id")
        if message_id is not None:
            try:
                self.api_call(
                    "pinChatMessage",
                    {"chat_id": self.channel_id, "message_id": message_id},
                )
            except CloudSyncError:
                log.warning(
                    "could not pin state message (missing Pin rights?) — "
                    "dashboard will read the latest post instead"
                )
        self.last_state_payload = envelope
        return message_id

    # --------------------------------------------------------- cloud -> core
    def poll_incoming(self) -> list[dict[str, Any]]:
        """Long-poll getUpdates and return applied envelopes.

        Handles channel_post (dashboard posted via Bot API) and web_app_data
        (Mini App sendData fallback). Malformed envelopes are ignored.
        """
        if not self.bot_token:
            return []
        result = self.api_call(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": GET_UPDATES_TIMEOUT,
                "allowed_updates": json.dumps(["message", "channel_post"]),
            },
        )
        applied: list[dict[str, Any]] = []
        for update in result or []:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            raw: Optional[str] = None
            if "channel_post" in update:
                raw = (update["channel_post"] or {}).get("text")
            elif "message" in update and "web_app_data" in update["message"]:
                raw = (update["message"]["web_app_data"] or {}).get("data")
            if not raw:
                continue
            envelope = self._parse_envelope(raw)
            if envelope is None:
                continue
            if self.apply_envelope(envelope):
                applied.append(envelope)
        return applied

    @staticmethod
    def _parse_envelope(raw: str) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) and "type" in data else None

    def apply_envelope(self, envelope: dict[str, Any]) -> bool:
        """Apply one dashboard envelope. Returns True when something changed."""
        kind = envelope.get("type")
        payload = envelope.get("payload") or {}
        if kind == "set_config":
            cameras = payload.get("cctv_rtsp_urls")
            prompt = payload.get("ai_target_prompt")
            changed = False
            if isinstance(cameras, list) and cameras != getattr(self, "cameras", None):
                self.cameras = cameras
                changed = True
            if isinstance(prompt, str) and prompt.strip():
                self.ai_target_prompt = prompt.strip()
                changed = True
            log.info("applied set_config (cameras=%d, prompt=%s)",
                     len(getattr(self, "cameras", []) or []),
                     bool(getattr(self, "ai_target_prompt", "")))
            return changed
        if kind == "set_subscription":
            expiry = payload.get("subscription_expiry")
            sender = envelope.get("user_id")
            if isinstance(expiry, (int, float)) and expiry > 0:
                if self.admin_id and int(sender or 0) != self.admin_id:
                    log.warning("rejected set_subscription from non-admin %s", sender)
                    return False
                self.subscription_expiry = int(expiry)
                log.info("subscription updated to %d", self.subscription_expiry)
                return True
        log.debug("ignored unknown envelope type: %s", kind)
        return False

    # -------------------------------------------------------- subscription
    def is_subscription_active(self) -> bool:
        """Firewall check used by the main loop."""
        return int(time.time()) < int(self.subscription_expiry)
