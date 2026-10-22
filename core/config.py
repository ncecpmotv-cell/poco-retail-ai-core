"""Configuration loading for the Retail AI Core daemon.

Loads `config.env` (KEY=VALUE) via python-dotenv when available and falls back
to plain os.environ, so the daemon also runs in environments where dotenv is
not installed. Every value has a sensible default; secrets stay in config.env
which is gitignored.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

try:  # optional dependency; not needed for plain env-var setups
    from dotenv import load_dotenv  # type: ignore

    load_dotenv("config.env")
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_json(name: str, default: Any) -> Any:
    raw = _env(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


@dataclass
class Config:
    """Typed view over the daemon configuration."""

    # Telegram bridge
    bot_token: str = ""
    state_channel_id: str = ""
    admin_id: int = 0
    user_id: int = 0

    # Retail analytics
    store_capacity: int = 50
    staffing_threshold: int = 15
    congested_at: float = 0.6
    critical_at: float = 0.9
    loop_interval_seconds: int = 60
    state_post_interval_seconds: int = 300

    # Local fallback config (used until the dashboard pushes set_config)
    local_cameras: list[dict[str, Any]] = field(default_factory=list)
    local_ai_prompt: str = (
        "You are a retail CCTV analyst. Describe the scene and count how many "
        'people are visible. Reply in strict JSON: {"count": <int>, '
        '"staff_present": <bool>, "congestion": "low|medium|high", '
        '"summary": "<short>"}'
    )
    default_subscription_expiry: int = 4102444800  # 2100-01-01

    # Paths
    db_path: str = "data/retail_ai.db"
    snapshot_dir: str = "snapshots"
    model_dir: str = "models"
    model_file: str = "MiniCPM-V-4_7-1B-Q4_K_M.gguf"
    mmproj_file: str = "mmproj-model-f16.gguf"
    minicpmv_cli_path: str = "llama.cpp/build/bin/llama-minicpmv-cli"
    log_file: str = "logs/retail_ai.log"
    inference_timeout_seconds: int = 120

    @classmethod
    def load(cls) -> "Config":
        """Build a Config from the environment."""
        return cls(
            bot_token=_env("BOT_TOKEN"),
            state_channel_id=_env("STATE_CHANNEL_ID"),
            admin_id=_env_int("ADMIN_ID", 0),
            user_id=_env_int("USER_ID", 0),
            store_capacity=_env_int("STORE_CAPACITY", 50),
            staffing_threshold=_env_int("STAFFING_THRESHOLD", 15),
            congested_at=_env_float("CONGESTED_AT", 0.6),
            critical_at=_env_float("CRITICAL_AT", 0.9),
            loop_interval_seconds=_env_int("LOOP_INTERVAL_SECONDS", 60),
            state_post_interval_seconds=_env_int("STATE_POST_INTERVAL_SECONDS", 300),
            local_cameras=_env_json("LOCAL_CAMERAS", []),
            local_ai_prompt=_env(
                "LOCAL_AI_PROMPT",
                "You are a retail CCTV analyst. Describe the scene and count "
                "how many people are visible. Reply in strict JSON: "
                '{"count": <int>, "staff_present": <bool>, '
                '"congestion": "low|medium|high", "summary": "<short>"}',
            ),
            default_subscription_expiry=_env_int(
                "DEFAULT_SUBSCRIPTION_EXPIRY", 4102444800
            ),
            db_path=_env("DB_PATH", "data/retail_ai.db"),
            snapshot_dir=_env("SNAPSHOT_DIR", "snapshots"),
            model_dir=_env("MODEL_DIR", "models"),
            model_file=_env("MODEL_FILE", "MiniCPM-V-4_7-1B-Q4_K_M.gguf"),
            mmproj_file=_env("MMPROJ_FILE", "mmproj-model-f16.gguf"),
            minicpmv_cli_path=_env(
                "MINICPMV_CLI_PATH", "llama.cpp/build/bin/llama-minicpmv-cli"
            ),
            log_file=_env("LOG_FILE", "logs/retail_ai.log"),
            inference_timeout_seconds=_env_int("INFERENCE_TIMEOUT_SECONDS", 120),
        )
