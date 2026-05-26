"""State orchestrator: background threads fetch usage data, UI consumes a snapshot.

Threading model:
  - Main thread: owns the tkinter UI loop (started elsewhere).
  - API thread: polls /api/oauth/usage (rate-limited, so every API_INTERVAL_SEC).
  - JSONL thread: re-parses ~/.claude/projects (cheap, every JSONL_INTERVAL_SEC).
  - All threads write to AppState fields under _lock; UI reads via snapshot().
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Callable

from jsonl_costs import UsageReport, build_report
from usage_api import UsageSnapshot, fetch_usage, load_oauth_token

log = logging.getLogger(__name__)

# Polling intervals. The OAuth usage endpoint is rate-limited to ~5 req per token,
# but a fresh token is issued ~every 8h so we have a budget of ~5/8h = ~1 req per 90 min.
# We're more aggressive than that and rely on the token-refresh flow to bail us out
# when we get 429ed. 360s = 6 min is what CodeZeno uses by default.
API_INTERVAL_SEC = 360
JSONL_INTERVAL_SEC = 30


@dataclass
class AppState:
    """Shared mutable state. Always accessed under lock when mutating."""

    usage: UsageSnapshot | None = None         # Most recent API snapshot, or None pre-first-fetch.
    usage_error: str = ""                      # Last error message, "" if last fetch was ok.
    report: UsageReport | None = None          # Most recent JSONL aggregation.
    report_error: str = ""
    last_api_fetch: float = 0.0
    last_jsonl_parse: float = 0.0
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
                report=self.state.report,
                report_error=self.state.report_error,
                last_api_fetch=self.state.last_api_fetch,
                last_jsonl_parse=self.state.last_jsonl_parse,
                alerted_5h=set(self.state.alerted_5h),
                alerted_7d=set(self.state.alerted_7d),
            )

    # ---------- background loops ----------

    def _api_loop(self) -> None:
        while not self._stop.is_set():
            try:
                token, _ = load_oauth_token()
                snap = fetch_usage(token)
                with self._lock:
                    self.state.usage = snap
                    self.state.usage_error = ""
                    self.state.last_api_fetch = time.time()
                self._check_thresholds(snap)
                self._on_change()
            except urllib.error.HTTPError as e:
                self._record_api_error(f"HTTP {e.code}: {e.reason}")
            except urllib.error.URLError as e:
                self._record_api_error(f"network: {e.reason}")
            except (FileNotFoundError, ValueError) as e:
                self._record_api_error(str(e))
            except Exception as e:  # noqa: BLE001 — last-resort safety
                self._record_api_error(f"{type(e).__name__}: {e}")
            # Wait for either the interval or a manual wake.
            self._api_wake.wait(timeout=API_INTERVAL_SEC)
            self._api_wake.clear()

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
