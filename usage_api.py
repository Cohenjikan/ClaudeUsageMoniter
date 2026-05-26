"""Read Claude Code OAuth credentials and call the /api/oauth/usage endpoint.

This is the data-layer module — pure functions, no UI.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Anthropic internal OAuth usage endpoint (undocumented but stable; used by Claude Code itself
# and by tools like CodeZeno's Claude-Code-Usage-Monitor and Maciek-roboblog's monitor).
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

# User-Agent must look like Claude Code or the endpoint hits a much tighter rate-limit bucket.
# See: github.com/anthropics/claude-code/issues/31021
DEFAULT_UA = "claude-code/2.0.0"

# Path to Claude Code's local OAuth credentials (Windows / macOS / Linux all use ~/.claude).
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


@dataclass
class UsageSnapshot:
    """One server-side reading of the user's subscription quota."""

    five_hour_pct: float          # 0–100, % of 5-hour rolling window consumed
    five_hour_reset: str          # ISO 8601 timestamp string
    seven_day_pct: float          # 0–100, % of weekly limit consumed
    seven_day_reset: str
    seven_day_opus_pct: float | None
    seven_day_sonnet_pct: float | None
    extra_usage_enabled: bool
    fetched_at: float             # Unix timestamp when this snapshot was taken
    raw: dict[str, Any]           # Full response body for debugging / future fields

    @property
    def five_hour_minutes_to_reset(self) -> int:
        return _minutes_until_iso(self.five_hour_reset)

    @property
    def seven_day_minutes_to_reset(self) -> int:
        return _minutes_until_iso(self.seven_day_reset)


def _minutes_until_iso(iso_ts: str) -> int:
    """Minutes from now until an ISO 8601 timestamp. Returns 0 if past."""
    from datetime import datetime, timezone
    try:
        # Handle "+00:00" suffix and microseconds. datetime.fromisoformat handles both since 3.11.
        target = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (target - now).total_seconds() / 60
        return max(0, int(delta))
    except (ValueError, TypeError):
        return 0


def load_oauth_token(creds_path: Path = CREDENTIALS_PATH) -> tuple[str, int]:
    """Read Claude Code's stored access token. Returns (token, expires_at_unix_ms).

    Raises FileNotFoundError if Claude Code has never been logged in.
    """
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Claude Code credentials not found at {creds_path}. "
            "Log into Claude Code first."
        )
    with open(creds_path, encoding="utf-8") as f:
        creds = json.load(f)
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt", 0)
    if not token:
        raise ValueError(
            f"No accessToken in {creds_path}. Try logging into Claude Code again."
        )
    return token, expires_at


def fetch_usage(token: str | None = None, *, user_agent: str = DEFAULT_UA) -> UsageSnapshot:
    """Fetch one snapshot from the /api/oauth/usage endpoint.

    Note: this endpoint is aggressively rate-limited (~5 req/token). Caller should
    cache and avoid polling more often than every 5+ minutes.
    """
    if token is None:
        token, _ = load_oauth_token()

    req = urllib.request.Request(
        USAGE_ENDPOINT,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    five = body.get("five_hour") or {}
    seven = body.get("seven_day") or {}
    seven_opus = body.get("seven_day_opus") or {}
    seven_sonnet = body.get("seven_day_sonnet") or {}
    extra = body.get("extra_usage") or {}

    return UsageSnapshot(
        five_hour_pct=float(five.get("utilization", 0) or 0),
        five_hour_reset=five.get("resets_at", ""),
        seven_day_pct=float(seven.get("utilization", 0) or 0),
        seven_day_reset=seven.get("resets_at", ""),
        seven_day_opus_pct=(float(seven_opus["utilization"]) if seven_opus.get("utilization") is not None else None),
        seven_day_sonnet_pct=(float(seven_sonnet["utilization"]) if seven_sonnet.get("utilization") is not None else None),
        extra_usage_enabled=bool(extra.get("is_enabled")),
        fetched_at=time.time(),
        raw=body,
    )


if __name__ == "__main__":
    # Smoke test: load token, call API, print structured snapshot.
    try:
        tok, exp_ms = load_oauth_token()
        print(f"[ok] token loaded (len={len(tok)}, expires in {(exp_ms/1000 - time.time())/3600:.1f}h)")
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(f"[fatal] {e}")

    try:
        snap = fetch_usage(tok)
    except urllib.error.HTTPError as e:
        print(f"[fatal] HTTP {e.code}: {e.reason}")
        print(e.read().decode("utf-8", errors="replace"))
        raise SystemExit(1)
    except urllib.error.URLError as e:
        raise SystemExit(f"[fatal] network: {e.reason}")

    print(f"  5h:  {snap.five_hour_pct:5.1f}%  resets in {snap.five_hour_minutes_to_reset} min")
    print(f"  7d:  {snap.seven_day_pct:5.1f}%  resets in {snap.seven_day_minutes_to_reset} min")
    if snap.seven_day_sonnet_pct is not None:
        print(f"  Son: {snap.seven_day_sonnet_pct:5.1f}%")
    if snap.seven_day_opus_pct is not None:
        print(f"  Opu: {snap.seven_day_opus_pct:5.1f}%")
    print(f"  extra_usage enabled: {snap.extra_usage_enabled}")
