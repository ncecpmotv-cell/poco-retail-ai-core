"""Envelope signing/verification for the Retail AI channel bus (stdlib only).

All dashboard->core envelopes may be HMAC-SHA256-signed with a shared
BRIDGE_SECRET. When the secret is configured, unsigned envelopes are rejected
(protects set_subscription / set_config against forgery by anyone who can post
in the channel). When no secret is configured, signing is skipped and unsigned
envelopes are accepted (backward-compatible zero-config mode).

Pure stdlib module so the unittest suite (and CI) can cover it without any
network or pip dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic serialization: sorted keys, compact separators."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def compute_signature(payload: dict[str, Any], secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_envelope(
    envelope_type: str,
    payload: dict[str, Any],
    secret: str,
    user_id: Optional[int] = None,
) -> dict[str, Any]:
    """Build a signed envelope: {type, user_id, payload, sig}."""
    envelope: dict[str, Any] = {"type": envelope_type, "payload": payload}
    if user_id is not None:
        envelope["user_id"] = int(user_id)
    if secret:
        envelope["sig"] = compute_signature(envelope, secret)
    return envelope


def verify_envelope(envelope: dict[str, Any], secret: str) -> bool:
    """True when the envelope is trustworthy.

    * secret configured -> a valid signature is REQUIRED
    * secret empty     -> accepted as-is (legacy/compat mode)
    """
    if not secret:
        return True
    sig = envelope.get("sig")
    if not isinstance(sig, str):
        return False
    unsigned = {k: v for k, v in envelope.items() if k != "sig"}
    expected = compute_signature(unsigned, secret)
    return hmac.compare_digest(expected, sig)
