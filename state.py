"""State orchestrator: background threads fetch usage data, UI consumes a snapshot.

Threading model:
  - Main thread: owns the tkinter UI loop (started elsewhere).
  - API thread: polls /api/oauth/usage (rate-limited, so every API_INTERVAL_SEC).
  - JSONL thread: re-parses ~/.claude/projects (cheap, every JSONL_INTERVAL_SEC).
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
from usage_api import (
    UsageSnapshot, fetch_usage, load_oauth_creds, snapshot_from_body,
)

log = logging.getLogger(__name__)

# Last-good usage snapshot persisted next to the scripts. Lets the UI show the
# most recent known quota (with an honest age) immediately on launch, instead of
# blanking until the first network fetch succeeds — which after an overnight boot
# always needs a token refresh first and can be delayed by a boot-time network
# race or a refresh cooldown.
USAGE_CACHE_PATH = Path(__file__).parent / "usage_cache.json"

# Fast-retry ladder (seconds) used when the first fetch hasn't succeeded yet or a
# network-class error occurs, so quota appears promptly once connectivity / the
# refresh cooldown clears. Indexed by consecutive-failure count; past the end we
# fall back to the normal API_INTERVAL_SEC cadence. HTTP 429 and invalid_grant
# are NOT laddered — they keep their long absolute backoff timestamps.
RETRY_LADDER_SEC = (5, 15, 30, 60)

# Polling intervals. The OAuth usage endpoint is rate-limited to ~5 req per token,
# but a fresh token is issued ~every 8h so we have a budget of ~5/8h = ~1 req per 90 min.
# We're more aggressive than that and rely on the token-refresh flow to bail us out
# when we get 429ed. 360s = 6 min is what CodeZeno uses by default.
API_INTERVAL_SEC = 360
JSONL_INTERVAL_SEC = 30
# When the on-disk access token is expired we deliberately DO NOT refresh it
# ourselves (see _do_fetch_cycle for the full rationale) — we wait for Claude
# Code to rotate it and re-read the file. Re-check the disk this often: it's a
# cheap local read with NO network call, so it costs nothing against the
# rate-limit budget, and it makes quota recover within ~this many seconds once
# Claude Code writes a fresh token.
TOKEN_RECHECK_SEC = 45
# Backoff applied after we get rate-limited (HTTP 429). The /api/oauth/usage
# endpoint allows roughly 5 requests per token lifetime; once we trip it, the
# only way out is a fresh token. Wait 15 minutes before trying again — gives
# Claude Code plenty of time to refresh the token through normal use.
RATE_LIMIT_BACKOFF_SEC = 900


@dataclass
class AppState:
    """Shared mutable state. Always accessed under lock when mutating."""

    usage: UsageSnapshot | None = None         # Most recent API snapshot, or None pre-first-fetch.
    usage_error: str = ""                      # Last error message, "" if last fetch was ok.
    # True when the last cycle found the on-disk access token expired (or the
    # server rejected it). We do NOT refresh it ourselves; the UI uses this to
    # show an actionable "use Claude Code to refresh" message instead of a bare
    # error, and to mark the displayed quota as stale.
    token_expired: bool = False
    report: UsageReport | None = None          # Most recent JSONL aggregation.
    report_error: str = ""
    last_api_fetch: float = 0.0
    last_jsonl_parse: float = 0.0
    # Don't call the API again until this Unix timestamp. Set when we hit a 429,
    # to avoid burning more of the per-token rate-limit budget.
    api_backoff_until: float = 0.0
    # Last alert state per threshold key — used to fire each threshold only once per crossing.
    alerted_5h: set[int] = field(default_factory=set)
    alerted_7d: set[int] = field(default_factory=set)


class Orchestrator:
    """Owns the two background threads and the shared state.

    Notification callback `on_alert(kind, pct)` is called when a threshold crossing happens.
      kind in {"5h_75", "5h_90", "5h_95", "7d_75", "7d_90", "7d_95"}.
    """

    # Threshold percentages that trigger an alert (once per crossing).
    THRESHOLDS = (75, 90, 95)

    def __init__(self, on_change: Callable[[], None] | None = None,
                 on_alert: Callable[[str, float], None] | None = None) -> None:
        self.state = AppState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._api_thread: threading.Thread | None = None
        self._jsonl_thread: threading.Thread | None = None
        self._on_change = on_change or (lambda: None)
        self._on_alert = on_alert or (lambda kind, pct: None)
        # Wake events let us trigger an immediate refresh outside the polling cadence.
        self._api_wake = threading.Event()
        self._jsonl_wake = threading.Event()
        # Consecutive API-fetch failures — drives the fast-retry ladder. Only the
        # api thread touches it, so no lock needed.
        self._consecutive_failures = 0
        # Seed the UI with the last good snapshot from disk so quota shows up
        # immediately (with an honest age) rather than blank until the first
        # network fetch lands.
        self._load_usage_cache()

    def start(self) -> None:
        self._stop.clear()
        self._api_thread = threading.Thread(target=self._api_loop, name="api", daemon=True)
        self._jsonl_thread = threading.Thread(target=self._jsonl_loop, name="jsonl", daemon=True)
        self._api_thread.start()
        self._jsonl_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._api_wake.set()
        self._jsonl_wake.set()

    def refresh_now(self) -> None:
        """Manual trigger — used by the 'Refresh' menu item. Wakes both loops."""
        self._api_wake.set()
        self._jsonl_wake.set()

    def snapshot(self) -> AppState:
        """Return a shallow copy of state for the UI to read without holding the lock."""
        with self._lock:
            # AppState is small; copy by reconstruction. The contained dataclasses are
            # immutable in practice (we replace them whole rather than mutate in place).
            return AppState(
                usage=self.state.usage,
                usage_error=self.state.usage_error,
                token_expired=self.state.token_expired,
                report=self.state.report,
                report_error=self.state.report_error,
                last_api_fetch=self.state.last_api_fetch,
                last_jsonl_parse=self.state.last_jsonl_parse,
                api_backoff_until=self.state.api_backoff_until,
                alerted_5h=set(self.state.alerted_5h),
                alerted_7d=set(self.state.alerted_7d),
            )

    # ---------- last-good snapshot persistence ----------

    def _load_usage_cache(self) -> None:
        """Seed state.usage from the on-disk last-good snapshot, if present and
        valid. Corrupt / missing cache is ignored silently — it's only an
        optimization, never a correctness dependency."""
        try:
            with open(USAGE_CACHE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            raw = data["raw"]
            fetched_at = float(data["fetched_at"])
            if not isinstance(raw, dict):
                return
            snap = snapshot_from_body(raw, fetched_at)
        except (OSError, ValueError, KeyError, TypeError):
            return
        # No lock needed: runs in __init__ before the threads start.
        self.state.usage = snap
        self.state.last_api_fetch = fetched_at
        log.info("seeded usage from cache (age %ds)", int(time.time() - fetched_at))

    def _save_usage_cache(self, snap: UsageSnapshot) -> None:
        """Atomically persist the last good snapshot (tmp + Path.replace, same
        crash-safe pattern as save_oauth_creds). Failures are non-fatal."""
        try:
            payload = json.dumps({"raw": snap.raw, "fetched_at": snap.fetched_at})
            tmp = USAGE_CACHE_PATH.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(USAGE_CACHE_PATH)
        except OSError as e:
            log.warning("failed to persist usage cache: %s", e)

    # ---------- background loops ----------

    # Outcome codes returned by _do_fetch_cycle, consumed by _api_loop to pick
    # the next wait. BACKOFF means an absolute backoff timestamp was already set
    # (429) and the loop top will honor it — don't ladder.
    _OUT_OK = "ok"
    _OUT_RETRYABLE = "retryable"       # network error, or any failure pre-first-success
    _OUT_BACKOFF = "backoff"           # absolute backoff already set (429)
    _OUT_TOKEN_EXPIRED = "token_exp"   # disk token expired/rejected — we wait, never refresh

    def _api_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            backoff_left = self.state.api_backoff_until - now
            if backoff_left > 0:
                # In backoff (after a 429 or while token is expired). Sleep
                # without burning more budget, but respond to manual wake-ups
                # by re-evaluating at the next iteration top.
                self._api_wake.wait(timeout=min(backoff_left, API_INTERVAL_SEC))
                self._api_wake.clear()
                continue

            try:
                outcome = self._do_fetch_cycle()
            except urllib.error.URLError as e:
                self._record_api_error(f"network: {e.reason}")
                outcome = self._OUT_RETRYABLE
            except (FileNotFoundError, ValueError) as e:
                self._record_api_error(str(e))
                # No creds / malformed creds: keep showing last-known data and
                # retry on the fast ladder while usage is still unknown.
                outcome = self._OUT_RETRYABLE if self.state.usage is None else self._OUT_OK
            except Exception as e:  # noqa: BLE001 — last-resort safety
                self._record_api_error(f"{type(e).__name__}: {e}")
                outcome = self._OUT_RETRYABLE if self.state.usage is None else self._OUT_OK

            self._api_wake.wait(timeout=self._next_api_wait(outcome))
            self._api_wake.clear()

    def _next_api_wait(self, outcome: str) -> float:
        """Compute the delay before the next fetch attempt from this cycle's
        outcome and the consecutive-failure count (A2.3 / A2.4)."""
        if outcome == self._OUT_OK:
            self._consecutive_failures = 0
            return API_INTERVAL_SEC
        if outcome == self._OUT_TOKEN_EXPIRED:
            # Token expired/rejected. We never refresh it ourselves; just poll
            # the disk frequently (cheap, no network) so we pick up the fresh
            # token the moment Claude Code rotates it. Reset the failure ladder —
            # this isn't a transient error, it's a wait-for-CC state.
            self._consecutive_failures = 0
            return TOKEN_RECHECK_SEC
        if outcome == self._OUT_BACKOFF:
            # 429 set an absolute api_backoff_until; the loop top enforces it.
            # Don't ladder; a normal-interval wait re-checks it.
            return API_INTERVAL_SEC
        # _OUT_RETRYABLE: climb the fast ladder, then cap at the normal interval.
        idx = self._consecutive_failures
        self._consecutive_failures += 1
        if idx < len(RETRY_LADDER_SEC):
            return RETRY_LADDER_SEC[idx]
        return API_INTERVAL_SEC

    def _do_fetch_cycle(self) -> str:
        """One API read cycle. READ-ONLY PIGGYBACK — we never refresh the token.

        Why we never refresh: the OAuth refresh_token in ~/.claude/.credentials.json
        is SHARED with Claude Code and ROTATES on every use. If this monitor
        refreshed it, the rotation would invalidate Claude Code's in-memory copy
        and can break the user's Claude Code login (it has, once). Conversely,
        when Claude Code refreshes, OUR disk copy becomes the fresh one and we
        simply pick it up on the next read — load_oauth_creds() re-reads the file
        every call. So the only safe model is: read whatever token CC maintains;
        if it's expired, WAIT for CC to rotate it (polling the disk cheaply) and
        surface a clear "stale / use Claude Code" state in the meantime. We never
        write credentials.json.

        Returns an _OUT_* outcome code so _api_loop can choose the next wait.
        """
        creds = load_oauth_creds()
        if creds.is_expired():
            # Do NOT refresh. Mark the token-expired state; the UI shows the
            # last-known quota labelled stale plus an actionable hint. We keep
            # re-reading the disk (TOKEN_RECHECK_SEC) and recover automatically
            # the moment Claude Code writes a fresh token.
            with self._lock:
                first = not self.state.token_expired
                self.state.token_expired = True
                self.state.usage_error = "token_expired"
            if first:
                log.info("access token expired; waiting for Claude Code to "
                         "rotate it (read-only piggyback — we never refresh it "
                         "ourselves to avoid breaking the CC login)")
            return self._OUT_TOKEN_EXPIRED

        try:
            snap = fetch_usage(creds.access_token)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Server rejected a locally-valid token. Same policy: do NOT
                # refresh — treat as expired and wait for CC to rotate it.
                with self._lock:
                    self.state.token_expired = True
                    self.state.usage_error = "token_rejected"
                log.info("API returned 401; treating as expired (no self-refresh)")
                return self._OUT_TOKEN_EXPIRED
            return self._handle_api_http_error(e)

        # Success path
        with self._lock:
            self.state.usage = snap
            self.state.usage_error = ""
            self.state.token_expired = False
            self.state.last_api_fetch = time.time()
        self._save_usage_cache(snap)
        self._check_thresholds(snap)
        self._on_change()
        return self._OUT_OK

    def _handle_api_http_error(self, e: urllib.error.HTTPError) -> str:
        """Record a usage-endpoint HTTP error, set backoff if appropriate, and
        return the outcome code for _api_loop's wait selection."""
        msg = f"HTTP {e.code}: {e.reason}"
        if e.code == 429:
            with self._lock:
                self.state.api_backoff_until = time.time() + RATE_LIMIT_BACKOFF_SEC
            msg += f" (backing off {RATE_LIMIT_BACKOFF_SEC // 60} min)"
            self._record_api_error(msg)
            return self._OUT_BACKOFF
        self._record_api_error(msg)
        # Non-429 HTTP error: ladder only while we still have nothing to show.
        return self._OUT_RETRYABLE if self.state.usage is None else self._OUT_OK

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

    def _record_api_error(self, msg: str) -> None:
        with self._lock:
            self.state.usage_error = msg
        log.warning("usage api fetch failed: %s", msg)

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
