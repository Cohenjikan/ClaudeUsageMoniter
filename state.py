"""State orchestrator: background threads gather usage data, UI consumes a snapshot.

Quota sources, in priority order:
  1. Claude Code statusLine bridge (statusline.py) — token-free, no network,
     updates on every status refresh while a Claude Code session is running.
  2. READ-ONLY poll of /api/oauth/usage — covers idle periods (and chat/web
     usage made while no Claude Code session is open) plus model-scoped weekly
     buckets. Uses whatever access token Claude Code currently has on disk and
     NEVER refreshes it. Can be switched off entirely.

Threading model:
  - Main thread: owns the tkinter UI loop (started elsewhere).
  - statusline thread: watches the capture file's mtime (cheap local stat).
  - api thread: polls the usage endpoint, throttled hard while the statusline
    source is fresh.
  - jsonl thread: re-parses ~/.claude/projects (cheap, every JSONL_INTERVAL_SEC).
  - All threads write to AppState fields under _lock; UI reads via snapshot().
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from jsonl_costs import UsageReport, build_report
from paths import app_dir
from statusline import CAPTURE_PATH, body_from_capture, read_capture
from usage_api import UsageSnapshot, fetch_usage, load_oauth_creds, snapshot_from_body

log = logging.getLogger(__name__)

# Last-good usage snapshot persisted next to the program. Lets the UI show the
# most recent known quota (with an honest age) immediately on launch.
USAGE_CACHE_PATH = app_dir() / "usage_cache.json"

# Fast-retry ladder (seconds) used when the first fetch hasn't succeeded yet or a
# network-class error occurs. Indexed by consecutive-failure count; past the end
# we fall back to the normal API_INTERVAL_SEC cadence. HTTP 429 is NOT laddered.
RETRY_LADDER_SEC = (5, 15, 30, 60)

# API polling cadence when it's the only live source. The endpoint is
# aggressively rate-limited (~5 req per token), 6 min is the community default.
API_INTERVAL_SEC = 360
# While the statusline source is fresh it already gives us live 5h/7d numbers;
# the API is then only consulted this often (for model-scoped buckets).
API_INTERVAL_WITH_STATUSLINE_SEC = 1800
# The statusline capture counts as "fresh" (a Claude Code session is active)
# for this long after its last update.
STATUSLINE_FRESH_SEC = 600
# How often the statusline thread stats the capture file. Local, no network.
STATUSLINE_POLL_SEC = 2
JSONL_INTERVAL_SEC = 30
# When the on-disk access token is expired we deliberately DO NOT refresh it
# (see usage_api's module docstring). We re-read the file this often — a cheap
# local read — and recover as soon as Claude Code writes a fresh token.
TOKEN_RECHECK_SEC = 45
# Backoff applied after HTTP 429 from the usage endpoint.
RATE_LIMIT_BACKOFF_SEC = 900

# raw-body keys carried over from the previous snapshot when a statusline
# capture (which only has five_hour / seven_day) replaces it, so model-scoped
# buckets learned from the API don't blink out between API polls.
_CARRY_OVER_KEYS = ("limits", "seven_day_opus", "seven_day_sonnet", "extra_usage")

SOURCE_STATUSLINE = "statusline"
SOURCE_API = "api"


@dataclass
class AppState:
    """Shared mutable state. Always accessed under lock when mutating."""

    usage: UsageSnapshot | None = None         # Most recent quota snapshot (either source).
    usage_error: str = ""                      # Last API error message, "" if ok.
    # True while the API fallback can't run because Claude Code's access token
    # is expired/rejected. Only meaningful when usage_source == "api".
    token_expired: bool = False
    report: UsageReport | None = None          # Most recent JSONL aggregation.
    report_error: str = ""
    # Timestamp of the data currently shown (API fetch time, or statusline
    # capture time). The UI's "updated Xm ago" / stale logic reads this.
    last_api_fetch: float = 0.0
    last_jsonl_parse: float = 0.0
    # Which source produced `usage`: "statusline", "api", or "" (none yet).
    usage_source: str = ""
    # Most recent statusline capture time seen (0 = never).
    last_statusline: float = 0.0
    # Most recent SUCCESSFUL API fetch (0 = never this run).
    last_api_success: float = 0.0
    api_enabled: bool = True
    # Don't call the API again until this Unix timestamp (after a 429).
    api_backoff_until: float = 0.0
    # Last alert state per threshold key — used to fire each threshold only once per crossing.
    alerted_5h: set[int] = field(default_factory=set)
    alerted_7d: set[int] = field(default_factory=set)
    # Same, for model-scoped weekly buckets (Fable, …), keyed by bucket name.
    alerted_scoped: dict[str, set[int]] = field(default_factory=dict)

    def statusline_fresh(self, now: float | None = None) -> bool:
        if not self.last_statusline:
            return False
        return ((now or time.time()) - self.last_statusline) < STATUSLINE_FRESH_SEC


class Orchestrator:
    """Owns the background threads and the shared state.

    Notification callback `on_alert(kind, pct)` is called when a threshold crossing happens.
      kind in {"5h_75", …, "7d_95"} or "scoped:<name>:<thresh>".
    """

    # Threshold percentages that trigger an alert (once per crossing).
    THRESHOLDS = (75, 90, 95)

    def __init__(self, on_change: Callable[[], None] | None = None,
                 on_alert: Callable[[str, float], None] | None = None, *,
                 api_enabled: bool = True,
                 capture_path: Path = CAPTURE_PATH,
                 cache_path: Path | None = None) -> None:
        self.state = AppState(api_enabled=api_enabled)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._on_change = on_change or (lambda: None)
        self._on_alert = on_alert or (lambda kind, pct: None)
        self._capture_path = capture_path
        self._cache_path = cache_path or USAGE_CACHE_PATH
        # Wake events let us trigger an immediate refresh outside the polling cadence.
        self._api_wake = threading.Event()
        self._jsonl_wake = threading.Event()
        self._sl_wake = threading.Event()
        # Set by refresh_now(): the next API cycle runs even if the statusline
        # source is fresh. Only the api thread clears it.
        self._force_api = False
        # Consecutive API-fetch failures — drives the fast-retry ladder. Only the
        # api thread touches it, so no lock needed.
        self._consecutive_failures = 0
        self._last_capture_mtime: int | None = None
        self._load_usage_cache()

    def start(self) -> None:
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._statusline_loop, name="statusline", daemon=True),
            threading.Thread(target=self._api_loop, name="api", daemon=True),
            threading.Thread(target=self._jsonl_loop, name="jsonl", daemon=True),
        ]
        for th in self._threads:
            th.start()

    def stop(self) -> None:
        self._stop.set()
        self._api_wake.set()
        self._jsonl_wake.set()
        self._sl_wake.set()

    def refresh_now(self) -> None:
        """Manual trigger — used by the 'Refresh' menu item. Wakes every loop and
        lets the API fallback run once even if the statusline source is fresh."""
        self._force_api = True
        self._api_wake.set()
        self._jsonl_wake.set()
        self._sl_wake.set()

    def set_api_enabled(self, enabled: bool) -> None:
        """Turn the read-only API fallback on/off at runtime."""
        with self._lock:
            self.state.api_enabled = bool(enabled)
            if not enabled:
                self.state.token_expired = False
                self.state.usage_error = ""
        self._api_wake.set()
        self._on_change()

    def snapshot(self) -> AppState:
        """Return a shallow copy of state for the UI to read without holding the lock."""
        with self._lock:
            s = self.state
            return AppState(
                usage=s.usage,
                usage_error=s.usage_error,
                token_expired=s.token_expired,
                report=s.report,
                report_error=s.report_error,
                last_api_fetch=s.last_api_fetch,
                last_jsonl_parse=s.last_jsonl_parse,
                usage_source=s.usage_source,
                last_statusline=s.last_statusline,
                last_api_success=s.last_api_success,
                api_enabled=s.api_enabled,
                api_backoff_until=s.api_backoff_until,
                alerted_5h=set(s.alerted_5h),
                alerted_7d=set(s.alerted_7d),
                alerted_scoped={k: set(v) for k, v in s.alerted_scoped.items()},
            )

    # ---------- last-good snapshot persistence ----------

    def _load_usage_cache(self) -> None:
        """Seed state.usage from the on-disk last-good snapshot, if present and
        valid. Purely an optimization — never a correctness dependency."""
        try:
            with open(self._cache_path, encoding="utf-8") as f:
                data = json.load(f)
            raw = data["raw"]
            fetched_at = float(data["fetched_at"])
            if not isinstance(raw, dict):
                return
            snap = snapshot_from_body(raw, fetched_at)
            source = data.get("source")
        except FileNotFoundError:
            return  # normal first run
        except Exception:  # noqa: BLE001 — must never abort startup
            log.warning("ignoring unreadable usage cache", exc_info=True)
            return
        # No lock needed: runs in __init__ before the threads start.
        self.state.usage = snap
        self.state.last_api_fetch = fetched_at
        self.state.usage_source = source if source in (SOURCE_STATUSLINE, SOURCE_API) else SOURCE_API
        log.info("seeded usage from cache (age %ds)", int(time.time() - fetched_at))

    def _save_usage_cache(self, snap: UsageSnapshot, source: str) -> None:
        """Atomically persist the last good snapshot. Failures are non-fatal."""
        try:
            payload = json.dumps({"raw": snap.raw, "fetched_at": snap.fetched_at,
                                  "source": source})
            tmp = self._cache_path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError as e:
            log.warning("failed to persist usage cache: %s", e)

    def _publish(self, snap: UsageSnapshot, source: str) -> None:
        """Install a new snapshot from either source and notify listeners."""
        with self._lock:
            self.state.usage = snap
            self.state.usage_source = source
            self.state.last_api_fetch = snap.fetched_at
            if source == SOURCE_API:
                self.state.usage_error = ""
                self.state.token_expired = False
                self.state.last_api_success = snap.fetched_at
        self._save_usage_cache(snap, source)
        self._check_thresholds(snap)
        self._on_change()

    # ---------- source 1: statusline bridge ----------

    def _statusline_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_statusline()
            except Exception:  # noqa: BLE001 — keep the loop alive
                log.exception("statusline poll failed")
            self._sl_wake.wait(timeout=STATUSLINE_POLL_SEC)
            self._sl_wake.clear()

    def poll_statusline(self) -> bool:
        """Ingest the capture file if it changed since the last look. Returns
        True when a new snapshot was published. Public for tests."""
        try:
            mtime = self._capture_path.stat().st_mtime_ns
        except OSError:
            return False
        if mtime == self._last_capture_mtime:
            return False
        self._last_capture_mtime = mtime
        rec = read_capture(self._capture_path)
        if rec is None:
            return False
        captured_at = float(rec["captured_at"])
        body = body_from_capture(rec)
        if body is None:
            return False
        with self._lock:
            if captured_at > self.state.last_statusline:
                self.state.last_statusline = captured_at
            cur = self.state.usage
            if cur is not None and cur.fetched_at >= captured_at:
                return False  # what we show is already newer
            if cur is not None and isinstance(cur.raw, dict):
                for key in _CARRY_OVER_KEYS:
                    if key not in body and key in cur.raw:
                        body[key] = cur.raw[key]
        snap = snapshot_from_body(body, captured_at)
        self._publish(snap, SOURCE_STATUSLINE)
        return True

    # ---------- source 2: read-only usage API ----------

    # Outcome codes returned by _do_fetch_cycle, consumed by _api_loop.
    _OUT_OK = "ok"
    _OUT_RETRYABLE = "retryable"       # network error, or any failure pre-first-success
    _OUT_BACKOFF = "backoff"           # absolute backoff already set (429)
    _OUT_TOKEN_EXPIRED = "token_exp"   # disk token expired/rejected — wait, never refresh

    def _api_hold_seconds(self, now: float) -> float:
        """Seconds the API fallback should keep waiting before its next request
        for reasons other than backoff (disabled, or statusline covering us)."""
        if not self.state.api_enabled:
            return 3600.0
        if self._force_api:
            return 0.0
        if self.state.statusline_fresh(now):
            left = API_INTERVAL_WITH_STATUSLINE_SEC - (now - self.state.last_api_success)
            return max(0.0, left)
        return 0.0

    def _api_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            backoff_left = self.state.api_backoff_until - now
            hold = self._api_hold_seconds(now)
            if backoff_left > 0 or hold > 0:
                # Re-evaluate at least every minute so we resume polling soon
                # after Claude Code closes (statusline goes stale) or a wake.
                self._api_wake.wait(timeout=min(max(backoff_left, hold), 60.0))
                self._api_wake.clear()
                continue
            self._force_api = False

            try:
                outcome = self._do_fetch_cycle()
            except urllib.error.URLError as e:
                self._record_api_error(f"network: {e.reason}")
                outcome = self._OUT_RETRYABLE
            except (FileNotFoundError, ValueError) as e:
                self._record_api_error(str(e))
                outcome = self._OUT_RETRYABLE if self.state.usage is None else self._OUT_OK
            except Exception as e:  # noqa: BLE001 — last-resort safety
                self._record_api_error(f"{type(e).__name__}: {e}")
                outcome = self._OUT_RETRYABLE if self.state.usage is None else self._OUT_OK

            self._api_wake.wait(timeout=self._next_api_wait(outcome))
            self._api_wake.clear()

    def _next_api_wait(self, outcome: str) -> float:
        """Delay before the next fetch attempt from this cycle's outcome."""
        if outcome == self._OUT_OK:
            self._consecutive_failures = 0
            return API_INTERVAL_SEC
        if outcome == self._OUT_TOKEN_EXPIRED:
            # Wait-for-Claude-Code state, not a transient error: cheap disk
            # re-reads, no network.
            self._consecutive_failures = 0
            return TOKEN_RECHECK_SEC
        if outcome == self._OUT_BACKOFF:
            return API_INTERVAL_SEC
        idx = self._consecutive_failures
        self._consecutive_failures += 1
        if idx < len(RETRY_LADDER_SEC):
            return RETRY_LADDER_SEC[idx]
        return API_INTERVAL_SEC

    def _do_fetch_cycle(self) -> str:
        """One READ-ONLY API cycle: read Claude Code's current access token from
        disk and call the usage endpoint with it. If the token is expired or
        rejected we do not refresh it — we wait for Claude Code to rotate it."""
        creds = load_oauth_creds()
        if creds.is_expired():
            self._mark_token_expired("token_expired")
            return self._OUT_TOKEN_EXPIRED
        try:
            snap = fetch_usage(creds.access_token)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                self._mark_token_expired("token_rejected")
                return self._OUT_TOKEN_EXPIRED
            return self._handle_api_http_error(e)
        self._publish(snap, SOURCE_API)
        return self._OUT_OK

    def _mark_token_expired(self, code: str) -> None:
        with self._lock:
            first = not self.state.token_expired
            self.state.token_expired = True
            self.state.usage_error = code
        if first:
            log.info("access token %s; waiting for Claude Code to rotate it "
                     "(read-only: this app never refreshes tokens)", code)
            self._on_change()

    def _handle_api_http_error(self, e: urllib.error.HTTPError) -> str:
        msg = f"HTTP {e.code}: {e.reason}"
        if e.code == 429:
            with self._lock:
                self.state.api_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SEC
            msg += f" (backing off {RATE_LIMIT_BACKOFF_SEC // 60} min)"
            self._record_api_error(msg)
            return self._OUT_BACKOFF
        self._record_api_error(msg)
        return self._OUT_RETRYABLE if self.state.usage is None else self._OUT_OK

    def _record_api_error(self, msg: str) -> None:
        with self._lock:
            self.state.usage_error = msg
        log.warning("usage api fetch failed: %s", msg)

    # ---------- local JSONL costs ----------

    def _jsonl_loop(self) -> None:
        while not self._stop.is_set():
            try:
                rpt = build_report()
                with self._lock:
                    self.state.report = rpt
                    self.state.report_error = ""
                    self.state.last_jsonl_parse = time.time()
                self._on_change()
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self.state.report_error = f"{type(e).__name__}: {e}"
                log.warning("jsonl parse failed: %s", e)
            self._jsonl_wake.wait(timeout=JSONL_INTERVAL_SEC)
            self._jsonl_wake.clear()

    def _check_thresholds(self, snap: UsageSnapshot) -> None:
        """Fire alert callbacks when crossing a threshold for the first time per window.

        Reset state when utilization drops back below the threshold (e.g. after a window reset),
        so the next crossing fires again.
        """
        with self._lock:
            for thresh in self.THRESHOLDS:
                # 5h
                if snap.five_hour_pct >= thresh and thresh not in self.state.alerted_5h:
                    self.state.alerted_5h.add(thresh)
                    self._on_alert(f"5h_{thresh}", snap.five_hour_pct)
                elif snap.five_hour_pct < thresh:
                    self.state.alerted_5h.discard(thresh)
                # 7d
                if snap.seven_day_pct >= thresh and thresh not in self.state.alerted_7d:
                    self.state.alerted_7d.add(thresh)
                    self._on_alert(f"7d_{thresh}", snap.seven_day_pct)
                elif snap.seven_day_pct < thresh:
                    self.state.alerted_7d.discard(thresh)
            # Model-scoped weekly buckets (e.g. Fable). Kind format
            # "scoped:<name>:<thresh>" — consumers parse the name back out.
            for sl in snap.scoped_limits:
                seen = self.state.alerted_scoped.setdefault(sl.name, set())
                for thresh in self.THRESHOLDS:
                    if sl.pct >= thresh and thresh not in seen:
                        seen.add(thresh)
                        self._on_alert(f"scoped:{sl.name}:{thresh}", sl.pct)
                    elif sl.pct < thresh:
                        seen.discard(thresh)
            # Forget buckets the server stopped sending. Unlike 5h/7d — always
            # present, so their marks self-clear via the discard branch — a
            # scoped bucket can vanish (e.g. idle after a weekly reset) with its
            # marks frozen at "already alerted", which would swallow the first
            # crossing when it comes back in a fresh window.
            present = {sl.name for sl in snap.scoped_limits}
            for name in [n for n in self.state.alerted_scoped if n not in present]:
                del self.state.alerted_scoped[name]


if __name__ == "__main__":
    # Smoke test: spin up orchestrator, wait for first fetch, print snapshot.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    changes: list[float] = []
    alerts: list[tuple[str, float]] = []

    orch = Orchestrator(
        on_change=lambda: changes.append(time.time()),
        on_alert=lambda k, p: alerts.append((k, p)),
    )
    orch.start()

    # Wait up to 8 seconds for both first fetches to land.
    deadline = time.time() + 8
    while time.time() < deadline:
        s = orch.snapshot()
        if s.usage is not None and s.report is not None:
            break
        time.sleep(0.2)

    s = orch.snapshot()
    print(f"\n=== State after {len(changes)} change events, {len(alerts)} alerts ===")
    if s.usage:
        print(f"  API: 5h={s.usage.five_hour_pct:.1f}%  7d={s.usage.seven_day_pct:.1f}%")
    else:
        print(f"  API error: {s.usage_error}")
    if s.report:
        print(f"  JSONL: today=${s.report.today.cost_usd:.2f}  month=${s.report.this_month.cost_usd:.2f}")
        print(f"         current session=${s.report.by_session.get(s.report.last_session_id, type(s.report.today)()).cost_usd:.4f}")
    else:
        print(f"  JSONL error: {s.report_error}")
    if alerts:
        print(f"  alerts fired: {alerts}")
    orch.stop()
