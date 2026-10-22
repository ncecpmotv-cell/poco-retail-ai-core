"""Sequential snapshot grabber (RTSP and HTTP MJPEG sources).

RAM-safety rules from the knowledge base:
  * one camera at a time (open -> grab -> release),
  * buffer size forced to 1 so we always get the *latest* frame,
  * no persistent VideoCapture objects across loop iterations.

RTSP is forced over TCP transport (reliable on mobile networks) and
open/read are bounded to a few seconds so a dead camera cannot stall the
loop. Public HTTP MJPEG URLs work too — anything cv2's FFmpeg backend opens.

cv2 is imported lazily inside functions so the rest of the codebase (and the
unittest suite) works without OpenCV installed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("retail_ai.stream")

# OpenCV FFmpeg backends that accept raw RTSP; CAP_FFMPEG is the default on
# Termux builds.
_OPEN_TIMEOUT_MS = 10_000


def grab_snapshot(
    url: str,
    camera_id: str,
    snapshot_dir: str = "snapshots",
    timeout_ms: int = _OPEN_TIMEOUT_MS,
) -> Optional[str]:
    """Grab one fresh JPEG from `url` and return its path (or None on failure).

    Supports RTSP and HTTP MJPEG sources. Uses CAP_PROP_BUFFERSIZE = 1 (KB
    requirement) to avoid serving stale buffered frames, forces RTSP over TCP
    (far more reliable on mobile networks), and bounds open/read time so a
    dead camera costs seconds, not 30s+ hangs.
    """
    import os

    import cv2  # lazy import — see module docstring

    if not url:
        log.warning("camera %s: empty URL, skipping", camera_id)
        return None

    # Read by the FFmpeg backend when the capture is created; harmless for
    # HTTP MJPEG sources and fixes RTSP stalls behind mobile carriers.
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

    out_dir = Path(snapshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{camera_id}_snapshot.jpg"

    open_ms = min(int(timeout_ms), 8000)
    cap = cv2.VideoCapture()
    try:
        # CAP_PROP_OPEN_TIMEOUT_MSEC / READ_TIMEOUT_MSEC need OpenCV >= 4.5.2
        cap.open(
            url,
            cv2.CAP_FFMPEG,
            [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_ms,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, open_ms,
            ],
        )
    except cv2.error:
        cap.open(url, cv2.CAP_FFMPEG)  # older OpenCV: no timeout params
    if not cap.isOpened():
        log.warning("camera %s: could not open stream", camera_id)
        return None

    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        frame = None
        # Drain a few decoder frames; keep the newest one.
        for _ in range(3):
            ok, candidate = cap.read()
            if not ok or candidate is None:
                break
            frame = candidate
        if frame is None:
            log.warning("camera %s: no frame within %dms", camera_id, open_ms)
            return None
        if not cv2.imwrite(str(out_path), frame):
            log.warning("camera %s: imwrite failed for %s", camera_id, out_path)
            return None
        log.debug("camera %s: snapshot saved to %s", camera_id, out_path)
        return str(out_path)
    finally:
        cap.release()


def snapshot_all(
    cameras: list[dict[str, Any]], snapshot_dir: str = "snapshots"
) -> dict[str, Optional[str]]:
    """Sequentially snapshot every camera; a failing camera never aborts the
    rest. Returns {camera_id: snapshot_path_or_None}."""
    results: dict[str, Optional[str]] = {}
    for cam in cameras:
        cam_id = str(cam.get("id", "unknown"))
        try:
            results[cam_id] = grab_snapshot(
                str(cam.get("url", "")), cam_id, snapshot_dir
            )
        except Exception as exc:  # noqa: BLE001 — isolate per-camera failures
            log.error("camera %s: unexpected error: %s", cam_id, exc)
            results[cam_id] = None
    return results
