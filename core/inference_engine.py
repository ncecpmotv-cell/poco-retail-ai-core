"""MiniCPM-V inference wrapper.

Runs the locally compiled `llama-mtmd-cli` binary (llama.cpp's unified
multimodal CLI; the older `llama-minicpmv-cli` is deprecated upstream) as a
subprocess — the KB design: native C++ inference, no heavy Python wrappers —
injects a camera-role-aware prompt and parses the cleaned stdout into
structured telemetry. A malformed model answer degrades to safe defaults
instead of crashing the loop.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Optional

log = logging.getLogger("retail_ai.inference")

ROLE_PROMPTS = {
    "entrance": (
        "This camera watches the store ENTRANCE. Count every person visible "
        "inside the entrance area."
    ),
    "cashier": (
        "This camera watches the CASHIER area. Count every person visible in "
        "the queue, including staff behind the counter (mark them as staff)."
    ),
    "aisle": (
        "This camera watches a shopping AISLE. Count every shopper visible."
    ),
    "general": "This is a general store camera. Count every person visible.",
}

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_NUM_RE = re.compile(r"(\d+)\s*(?:people|persons|person|individuals)?")


def build_prompt(camera_role: str, target_prompt: str) -> str:
    """Compose the full prompt sent to the vision model."""
    role_instruction = ROLE_PROMPTS.get(
        (camera_role or "general").strip().lower(), ROLE_PROMPTS["general"]
    )
    base = target_prompt.strip() or (
        "Describe the scene and count the people. Reply in strict JSON: "
        '{"count": <int>, "staff_present": <bool>, '
        '"congestion": "low|medium|high", "summary": "<short>"}'
    )
    return f"{role_instruction}\n{base}"


def parse_inference_output(text: str) -> dict[str, Any]:
    """Turn raw model stdout into structured telemetry with safe defaults."""
    raw = (text or "").strip()
    result: dict[str, Any] = {
        "raw_text": raw[:2000],
        "count": 0,
        "congestion_level": "unknown",
        "staff_present": False,
    }
    # 1) Prefer a JSON object anywhere in the output.
    match = _JSON_RE.search(raw)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                if isinstance(data.get("count"), (int, float)):
                    result["count"] = max(0, int(data["count"]))
                if isinstance(data.get("staff_present"), bool):
                    result["staff_present"] = data["staff_present"]
                elif isinstance(data.get("staff"), bool):
                    result["staff_present"] = data["staff"]
                if isinstance(data.get("congestion"), str):
                    result["congestion_level"] = data["congestion"].lower()
                if isinstance(data.get("summary"), str):
                    result["summary"] = data["summary"][:500]
                return result
        except json.JSONDecodeError:
            pass
    # 2) Fallback: "N people"-style pattern.
    for m in _NUM_RE.finditer(raw):
        n = int(m.group(1))
        if 0 <= n <= 5000:
            result["count"] = max(result["count"], n)
    return result


def run_inference(
    cli_path: str,
    model_dir: str,
    model_file: str,
    image_path: str,
    prompt: str,
    timeout_seconds: int = 120,
    mmproj_file: str = "mmproj-model-f16.gguf",
) -> Optional[dict[str, Any]]:
    """Execute the CLI once and return parsed telemetry, or None on failure."""
    model_path = f"{model_dir.rstrip('/')}/{model_file}"
    mmproj_path = f"{model_dir.rstrip('/')}/{mmproj_file}"
    cmd = [
        cli_path,
        "-m", model_path,
        "--mmproj", mmproj_path,
        "--image", image_path,
        "-p", prompt,
        "--temp", "0.1",
        "-n", "96",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        log.error("inference binary not found: %s", cli_path)
        return None
    except subprocess.TimeoutExpired:
        log.error("inference timed out after %ds for %s", timeout_seconds, image_path)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("inference failed: %s", exc)
        return None

    if proc.returncode != 0:
        err = ((proc.stderr or "").strip() or (proc.stdout or "").strip())[-400:]
        log.error("inference exited %d: %s", proc.returncode, err)
        return None

    return parse_inference_output(proc.stdout)
