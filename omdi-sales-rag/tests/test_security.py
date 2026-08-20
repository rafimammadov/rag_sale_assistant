from __future__ import annotations

import unittest

from app.services.security import (
    UnsafeUrlError,
    safe_filename,
    sign_action_token,
    validate_public_url,
    verify_action_token,
)


class SecurityTests(unittest.TestCase):
    def test_action_token_round_trip_and_tamper_detection(self) -> None:
        token = sign_action_token({"order_id": "abc", "action": "review"}, "secret")
        payload = verify_action_token(token, "secret")
        self.assertEqual(payload["order_id"], "abc")
        with self.assertRaises(ValueError):
            verify_action_token(token + "x", "secret")

    def test_non_http_scheme_is_blocked(self) -> None:
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("file:///etc/passwd")

    def test_uploaded_filename_is_reduced_to_basename(self) -> None:
        self.assertEqual(safe_filename("../../catalog?.pdf"), "catalog_.pdf")


if __name__ == "__main__":
    unittest.main()

