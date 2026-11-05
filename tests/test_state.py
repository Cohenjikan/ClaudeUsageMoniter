"""Orchestrator tests: statusline ingestion, source merging, read-only API."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import state  # noqa: E402
import statusline  # noqa: E402
import usage_api  # noqa: E402
from usage_api import OAuthCreds, snapshot_from_body  # noqa: E402

API_BODY = {
    "five_hour": {"utilization": 20.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    "seven_day": {"utilization": 50.0, "resets_at": "2099-01-05T00:00:00+00:00"},
    "limits": [{"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}},
                "percent": 40, "resets_at": "2099-01-05T00:00:00+00:00"}],
}


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.capture = d / "statusline.json"
        self.cache = d / "usage_cache.json"
        self.alerts: list[tuple[str, float]] = []
        self.orch = state.Orchestrator(
            on_alert=lambda k, p: self.alerts.append((k, p)),
            capture_path=self.capture, cache_path=self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_capture(self, five, seven, captured_at):
        statusline.write_capture({"captured_at": captured_at, "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": 4070908800},
            "seven_day": {"used_percentage": seven, "resets_at": 4071254400},
        }}, self.capture)

    def test_statusline_capture_is_published(self):
        now = time.time()
        self._write_capture(77, 30, now)
        self.assertTrue(self.orch.poll_statusline())
        s = self.orch.snapshot()
        self.assertEqual(s.usage_source, "statusline")
        self.assertEqual(s.usage.five_hour_pct, 77.0)
        self.assertEqual(s.last_api_fetch, now)
        self.assertTrue(s.statusline_fresh())
        self.assertIn(("5h_75", 77.0), self.alerts)
        # Unchanged file -> no re-publish.
        self.assertFalse(self.orch.poll_statusline())
        # Cache written with its source.
        cached = json.loads(self.cache.read_text())
        self.assertEqual(cached["source"], "statusline")

    def test_older_capture_does_not_override_newer_api_data(self):
        self.orch._publish(snapshot_from_body(API_BODY, time.time()), state.SOURCE_API)
        self._write_capture(99, 99, time.time() - 120)
        self.assertFalse(self.orch.poll_statusline())
        self.assertEqual(self.orch.snapshot().usage.five_hour_pct, 20.0)

    def test_scoped_buckets_carry_over_from_api(self):
        self.orch._publish(snapshot_from_body(API_BODY, time.time() - 60), state.SOURCE_API)
        self._write_capture(25, 51, time.time())
        self.assertTrue(self.orch.poll_statusline())
        u = self.orch.snapshot().usage
        self.assertEqual(u.five_hour_pct, 25.0)
        self.assertEqual([sl.name for sl in u.scoped_limits], ["Fable"])

    def test_cache_seed_roundtrip(self):
        self._write_capture(12, 34, time.time())
        self.orch.poll_statusline()
        again = state.Orchestrator(capture_path=self.capture, cache_path=self.cache)
        s = again.snapshot()
        self.assertEqual(s.usage.seven_day_pct, 34.0)
        self.assertEqual(s.usage_source, "statusline")

    def test_api_held_while_statusline_fresh(self):
        now = time.time()
        self.orch.state.last_statusline = now
        self.orch.state.last_api_success = now - 60
        hold = self.orch._api_hold_seconds(now)
        self.assertAlmostEqual(hold, state.API_INTERVAL_WITH_STATUSLINE_SEC - 60, delta=1)
        # Manual refresh forces one API call.
        self.orch.refresh_now()
        self.assertEqual(self.orch._api_hold_seconds(now), 0.0)
        self.orch._force_api = False
        # Stale statusline -> API polls normally.
        self.orch.state.last_statusline = now - state.STATUSLINE_FRESH_SEC - 1
        self.assertEqual(self.orch._api_hold_seconds(now), 0.0)
        # Disabled -> always held.
        self.orch.set_api_enabled(False)
        self.assertGreater(self.orch._api_hold_seconds(now), 0)

    def test_expired_token_is_never_refreshed_or_sent(self):
        expired = OAuthCreds(access_token="tok", expires_at_unix=time.time() - 10)
        with mock.patch.object(state, "load_oauth_creds", return_value=expired), \
                mock.patch.object(state, "fetch_usage") as fetch:
            out = self.orch._do_fetch_cycle()
        self.assertEqual(out, self.orch._OUT_TOKEN_EXPIRED)
        fetch.assert_not_called()
        self.assertTrue(self.orch.snapshot().token_expired)
        self.assertEqual(self.orch._next_api_wait(out), state.TOKEN_RECHECK_SEC)

    def test_401_waits_for_claude_code(self):
        valid = OAuthCreds(access_token="tok", expires_at_unix=time.time() + 3600)
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch.object(state, "load_oauth_creds", return_value=valid), \
                mock.patch.object(state, "fetch_usage", side_effect=err):
            out = self.orch._do_fetch_cycle()
        self.assertEqual(out, self.orch._OUT_TOKEN_EXPIRED)

    def test_api_success_clears_token_state(self):
        valid = OAuthCreds(access_token="tok", expires_at_unix=time.time() + 3600)
        self.orch.state.token_expired = True
        snap = snapshot_from_body(API_BODY, time.time())
        with mock.patch.object(state, "load_oauth_creds", return_value=valid), \
                mock.patch.object(state, "fetch_usage", return_value=snap):
            self.assertEqual(self.orch._do_fetch_cycle(), self.orch._OUT_OK)
        s = self.orch.snapshot()
        self.assertFalse(s.token_expired)
        self.assertEqual(s.usage_source, "api")
        self.assertGreater(s.last_api_success, 0)

    def test_usage_api_has_no_token_writing_code(self):
        for name in ("refresh_oauth", "save_oauth_creds", "refresh_and_save"):
            self.assertFalse(hasattr(usage_api, name), name)


if __name__ == "__main__":
    unittest.main()
