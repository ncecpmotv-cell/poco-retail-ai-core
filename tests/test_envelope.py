"""Tests for envelope signing/verification (stdlib only)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.envelope import (  # noqa: E402
    canonical_json,
    compute_signature,
    sign_envelope,
    verify_envelope,
)


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.secret = "test-secret-123"
        self.payload = {"cctv_rtsp_urls": [{"id": "cam1", "url": "rtsp://x"}],
                        "ai_target_prompt": "count"}

    def test_canonical_json_is_deterministic(self):
        a = canonical_json({"b": 1, "a": 2})
        b = canonical_json({"a": 2, "b": 1})
        self.assertEqual(a, b)
        self.assertEqual(a, '{"a":2,"b":1}')

    def test_sign_then_verify_roundtrip(self):
        env = sign_envelope("set_config", self.payload, self.secret, user_id=42)
        self.assertIn("sig", env)
        self.assertTrue(verify_envelope(env, self.secret))

    def test_tampered_envelope_rejected(self):
        env = sign_envelope("set_subscription",
                            {"subscription_expiry": 9999999999},
                            self.secret, user_id=42)
        env["payload"]["subscription_expiry"] = 1  # tamper
        self.assertFalse(verify_envelope(env, self.secret))

    def test_missing_signature_rejected_when_secret_set(self):
        env = {"type": "set_config", "payload": self.payload}
        self.assertFalse(verify_envelope(env, self.secret))

    def test_unsigned_accepted_when_no_secret(self):
        env = {"type": "set_config", "payload": self.payload}
        self.assertTrue(verify_envelope(env, ""))

    def test_wrong_secret_rejected(self):
        env = sign_envelope("set_config", self.payload, self.secret)
        self.assertFalse(verify_envelope(env, "different-secret"))


if __name__ == "__main__":
    unittest.main()
