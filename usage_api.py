"""Read Claude Code OAuth credentials and call the /api/oauth/usage endpoint.

This is the data-layer module — pure functions, no UI.

STRICTLY READ-ONLY. This module never refreshes the OAuth token and never
writes ~/.claude/.credentials.json. The refresh_token in that file is shared
with Claude Code and rotates on every use; refreshing it from here would race
Claude Code and can log the user out, and it would also mean impersonating
Claude Code's OAuth client. When the access token Claude Code maintains is
expired we simply wait for Claude Code to rotate it.

The usage endpoint is only the FALLBACK quota source. The primary source is the
Claude Code statusLine bridge (see statusline.py), which needs no token at all.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from paths import CLAUDE_DIR

# Anthropic internal OAuth usage endpoint (undocumented but stable; used by Claude Code itself
# and by tools like CodeZeno's Claude-Code-Usage-Monitor and Maciek-roboblog's monitor).
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

# User-Agent must look like Claude Code or the endpoint hits a much tighter rate-limit bucket.
# See: github.com/anthropics/claude-code/issues/31021
DEFAULT_UA = "claude-code/2.0.0"

# Path to Claude Code's local OAuth credentials (Windows / macOS / Linux all use ~/.claude).
CREDENTIALS_PATH = CLAUDE_DIR / ".credentials.json"


@dataclass
class ScopedLimit:
    """A model-scoped (or surface-scoped) weekly quota bucket.

    Mid-2026 the usage endpoint grew a generic `limits` array; model-specific
    weekly quotas (e.g. the separate Fable bucket) arrive as kind
    "weekly_scoped" entries there, replacing the old fixed seven_day_opus /
    seven_day_sonnet top-level fields (now null). We parse them generically so
    new model buckets appear without code changes.
    """

    name: str                     # server display name, e.g. "Fable"
    pct: float                    # 0–100 utilization
    reset: str                    # ISO 8601 resets_at, "" if absent
    severity: str = "normal"      # server-reported: normal / warning / critical

    @property
    def minutes_to_reset(self) -> int:
        return _minutes_until_iso(self.reset)

    @property
    def active(self) -> bool:
        return bool(self.reset)


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
    scoped_limits: tuple[ScopedLimit, ...] = ()  # model-scoped weekly buckets (Fable, …)

    @property
    def five_hour_minutes_to_reset(self) -> int:
        return _minutes_until_iso(self.five_hour_reset)

    @property
    def seven_day_minutes_to_reset(self) -> int:
        return _minutes_until_iso(self.seven_day_reset)

    @property
    def five_hour_active(self) -> bool:
        """True iff a 5h block is actually running. When no block is active the
        endpoint returns utilization 0 with an empty/None resets_at; a non-empty
        resets_at is the reliable signal that the window is live."""
        return bool(self.five_hour_reset)

    @property
    def seven_day_active(self) -> bool:
        """True iff the weekly window is live (non-empty resets_at). See
        five_hour_active for the rationale."""
        return bool(self.seven_day_reset)


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


@dataclass
class OAuthCreds:
    """Claude Code's stored OAuth state, with a convenience expiry check."""
    access_token: str
    expires_at_unix: float    # Unix seconds (not ms)
    scopes: tuple[str, ...] = ()

    def is_expired(self, buffer_sec: float = 30) -> bool:
        """True if the access token is past expiry, including a small safety buffer
        so we don't fire a request that will arrive after expiry."""
        return time.time() >= self.expires_at_unix - buffer_sec


def load_oauth_token(creds_path: Path = CREDENTIALS_PATH) -> tuple[str, int]:
    """Back-compat wrapper — returns (access_token, expires_at_unix_ms)."""
    c = load_oauth_creds(creds_path)
    return c.access_token, int(c.expires_at_unix * 1000)


def load_oauth_creds(creds_path: Path = CREDENTIALS_PATH) -> OAuthCreds:
    """Read Claude Code's stored OAuth state — re-reads from disk every call so
    we automatically pick up tokens refreshed by Claude Code itself.

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
    access = oauth.get("accessToken") or ""
    if not access:
        raise ValueError(
            f"No accessToken in {creds_path}. Try logging into Claude Code again."
        )
    return OAuthCreds(
        access_token=access,
        expires_at_unix=(oauth.get("expiresAt") or 0) / 1000,
        scopes=tuple(oauth.get("scopes") or ()),
    )


def _parse_scoped_limits(body: dict[str, Any]) -> tuple[ScopedLimit, ...]:
    """Extract model-scoped weekly buckets from the `limits` array, folding in
    the legacy seven_day_opus / seven_day_sonnet fields when they still carry
    data and no equivalent scoped entry exists (transition-period responses).

    Every field is type-normalized rather than merely None-checked: this parses
    an undocumented endpoint whose shape we expect to keep drifting, and an
    AttributeError here would abort snapshot_from_body entirely — throwing away
    the perfectly valid five_hour / seven_day data in the same response.

    Names are deduped (highest pct wins) because the scope object has both a
    `model` and a `surface` dimension: two entries for one model on different
    surfaces both resolve to that model's name, and downstream alert state is
    keyed by name — a duplicate would let the lower-pct entry clear threshold
    marks the higher-pct one just set, re-firing toasts on every poll.
    """
    scoped: list[ScopedLimit] = []
    for lim in body.get("limits") or ():
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        scope = lim.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        model = scope.get("model")
        if isinstance(model, str):
            model = {"id": model}      # bare model id — keep it as the name
        elif not isinstance(model, dict):
            model = {}
        surface = scope.get("surface")
        if isinstance(surface, dict):
            surface = surface.get("display_name") or surface.get("id")
        elif not isinstance(surface, str):
            surface = None
        name = model.get("display_name") or model.get("id") or surface
        if not name or not isinstance(name, str):
            continue
        try:
            pct = float(lim.get("percent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        reset = lim.get("resets_at")
        scoped.append(ScopedLimit(
            name=name,
            pct=pct,
            reset=reset if isinstance(reset, str) else "",
            severity=str(lim.get("severity") or "normal"),
        ))
    for legacy_key, legacy_name in (("seven_day_opus", "Opus"),
                                    ("seven_day_sonnet", "Sonnet")):
        obj = body.get(legacy_key)
        if not isinstance(obj, dict) or obj.get("utilization") is None:
            continue
        if any(s.name.lower() == legacy_name.lower() for s in scoped):
            continue
        try:
            legacy_pct = float(obj["utilization"])
        except (TypeError, ValueError):
            continue
        reset = obj.get("resets_at")
        scoped.append(ScopedLimit(
            name=legacy_name,
            pct=legacy_pct,
            reset=reset if isinstance(reset, str) else "",
        ))
    # Collapse same-name entries, keeping the highest utilization. Order of
    # first appearance is preserved so the UI row order stays stable.
    deduped: dict[str, ScopedLimit] = {}
    for sl in scoped:
        prev = deduped.get(sl.name)
        if prev is None:
            deduped[sl.name] = sl
        elif sl.pct > prev.pct:
            # Don't let the winner's blank resets_at discard a reset time the
            # loser did carry — the UI needs one to show a countdown.
            deduped[sl.name] = replace(sl, reset=sl.reset or prev.reset)
        elif not prev.reset and sl.reset:
            deduped[sl.name] = replace(prev, reset=sl.reset)
    return tuple(deduped.values())


def snapshot_from_body(body: dict[str, Any], fetched_at: float) -> UsageSnapshot:
    """Build a UsageSnapshot from a raw /api/oauth/usage response body.

    Single parsing path shared by the live fetch and the on-disk last-snapshot
    cache, so a snapshot reconstructed from `usage_cache.json` is byte-for-byte
    equivalent to one parsed fresh from the network.

    Every section is type-checked, not just None-checked: a wrong-typed field
    from this undocumented endpoint must degrade that one value, never raise
    and throw away the rest of an otherwise valid response.
    """
    def section(key: str) -> dict[str, Any]:
        val = body.get(key)
        return val if isinstance(val, dict) else {}

    def pct(obj: dict[str, Any], key: str = "utilization") -> float | None:
        val = obj.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def iso(obj: dict[str, Any]) -> str:
        val = obj.get("resets_at")
        return val if isinstance(val, str) else ""

    five = section("five_hour")
    seven = section("seven_day")
    extra = section("extra_usage")

    return UsageSnapshot(
        five_hour_pct=pct(five) or 0.0,
        five_hour_reset=iso(five),
        seven_day_pct=pct(seven) or 0.0,
        seven_day_reset=iso(seven),
        seven_day_opus_pct=pct(section("seven_day_opus")),
        seven_day_sonnet_pct=pct(section("seven_day_sonnet")),
        extra_usage_enabled=bool(extra.get("is_enabled")),
        fetched_at=fetched_at,
        raw=body,
        scoped_limits=_parse_scoped_limits(body),
    )


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

    return snapshot_from_body(body, time.time())


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
    for sl in snap.scoped_limits:
        print(f"  {sl.name}: {sl.pct:5.1f}%  ({sl.severity})  resets in {sl.minutes_to_reset} min")
    print(f"  extra_usage enabled: {snap.extra_usage_enabled}")
