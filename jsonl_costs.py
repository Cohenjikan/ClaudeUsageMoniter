"""Parse Claude Code's local JSONL transcripts to compute usage and cost.

Pure functions — no UI, no network. Tested against real data layout from
~/.claude/projects/<encoded-path>/<session-uuid>.jsonl.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# Anthropic pricing per million tokens, USD. Captured from current published rates.
# Keys are matched as substrings against the model name returned in the JSONL `message.model` field.
# `cache_5m` and `cache_1h` are the two tiers of cache-creation pricing
# (5-minute ephemeral vs 1-hour ephemeral, where 1h is more expensive).
PRICING: dict[str, dict[str, float]] = {
    "opus-4":   {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_5m": 18.75, "cache_1h": 30.0},
    "sonnet-4": {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_5m":  3.75, "cache_1h":  6.0},
    "haiku-4":  {"input":  1.0, "output":  5.0, "cache_read": 0.10, "cache_5m":  1.25, "cache_1h":  2.0},
    # Fallback for unknown / future models — assume Sonnet pricing (mid-tier).
    "_default": {"input":  3.0, "output": 15.0, "cache_read": 0.30, "cache_5m":  3.75, "cache_1h":  6.0},
}


def _price_for_model(model: str) -> dict[str, float]:
    """Pick a pricing row by substring-matching the model name."""
    if not model:
        return PRICING["_default"]
    m = model.lower()
    for key, row in PRICING.items():
        if key != "_default" and key in m:
            return row
    return PRICING["_default"]


@dataclass
class TurnCost:
    """Cost breakdown for one assistant message turn."""
    timestamp: datetime           # When the message was generated (UTC).
    project_cwd: str              # Real working directory from the JSONL record.
    session_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_5m_tokens: int = 0
    cache_1h_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Aggregate:
    """Sum of TurnCost over some slice (a day, a project, a session...)."""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_5m_tokens: int = 0
    cache_1h_tokens: int = 0
    turns: int = 0

    def add(self, t: TurnCost) -> None:
        self.cost_usd += t.cost_usd
        self.input_tokens += t.input_tokens
        self.output_tokens += t.output_tokens
        self.cache_read_tokens += t.cache_read_tokens
        self.cache_5m_tokens += t.cache_5m_tokens
        self.cache_1h_tokens += t.cache_1h_tokens
        self.turns += 1


@dataclass
class UsageReport:
    """All aggregations we want to show in the UI."""
    today: Aggregate = field(default_factory=Aggregate)
    this_month: Aggregate = field(default_factory=Aggregate)
    by_project: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))
    by_session: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))
    last_session_id: str = ""     # Heuristic: the session-id of the most-recent turn we saw.
    last_session_at: datetime | None = None
    all_time: Aggregate = field(default_factory=Aggregate)


def _parse_turn(obj: dict, fallback_session: str) -> TurnCost | None:
    """Convert one raw JSONL record into a TurnCost, or None if not a billable turn."""
    msg = obj.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return None
    # Some entries (tool results, user messages) don't have a model — skip silently.
    model = msg.get("model") or ""
    if not model:
        return None

    # cache_creation may be present as a dict with the 5m/1h split. If not, treat all cache
    # creation as 5m (the cheaper tier — conservative estimate).
    cache_creation = usage.get("cache_creation") or {}
    cache_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
    cache_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
    total_cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    # If split is missing but total is present, default everything to 5m.
    if (cache_5m + cache_1h) == 0 and total_cache_creation > 0:
        cache_5m = total_cache_creation

    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cread = int(usage.get("cache_read_input_tokens") or 0)

    price = _price_for_model(model)
    cost = (
        inp * price["input"]
        + out * price["output"]
        + cread * price["cache_read"]
        + cache_5m * price["cache_5m"]
        + cache_1h * price["cache_1h"]
    ) / 1_000_000

    # Timestamp — JSONL uses ISO 8601 with "Z" suffix.
    ts_str = obj.get("timestamp") or ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        ts = datetime.now(timezone.utc)

    return TurnCost(
        timestamp=ts,
        project_cwd=obj.get("cwd") or "",
        session_id=obj.get("sessionId") or fallback_session,
        model=model,
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cread,
        cache_5m_tokens=cache_5m,
        cache_1h_tokens=cache_1h,
        cost_usd=cost,
    )


def iter_turns(root: Path = PROJECTS_ROOT) -> Iterable[TurnCost]:
    """Yield every billable TurnCost across all session files under root."""
    if not root.exists():
        return
    for session_file in root.rglob("*.jsonl"):
        # Fallback session id from filename if record doesn't carry one.
        fallback_sid = session_file.stem
        try:
            with open(session_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    turn = _parse_turn(obj, fallback_sid)
                    if turn is not None:
                        yield turn
        except (PermissionError, OSError):
            continue


def build_report(root: Path = PROJECTS_ROOT, now: datetime | None = None) -> UsageReport:
    """Aggregate all turns into the structured report the UI consumes."""
    if now is None:
        now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    rpt = UsageReport()
    for turn in iter_turns(root):
        rpt.all_time.add(turn)
        if turn.timestamp >= today_start:
            rpt.today.add(turn)
        if turn.timestamp >= month_start:
            rpt.this_month.add(turn)
        rpt.by_project[turn.project_cwd or "<unknown>"].add(turn)
        rpt.by_session[turn.session_id].add(turn)
        if rpt.last_session_at is None or turn.timestamp > rpt.last_session_at:
            rpt.last_session_at = turn.timestamp
            rpt.last_session_id = turn.session_id

    return rpt


if __name__ == "__main__":
    # Smoke test: build the report, print summary.
    import time
    t0 = time.time()
    rpt = build_report()
    dt = time.time() - t0

    print(f"[ok] parsed {rpt.all_time.turns} turns in {dt:.2f}s")
    print(f"  all-time cost:    ${rpt.all_time.cost_usd:>10,.2f}")
    print(f"  this month:       ${rpt.this_month.cost_usd:>10,.2f}  ({rpt.this_month.turns} turns)")
    print(f"  today:            ${rpt.today.cost_usd:>10,.2f}  ({rpt.today.turns} turns)")
    print()
    print("=== top 10 projects by cost ===")
    top = sorted(rpt.by_project.items(), key=lambda kv: kv[1].cost_usd, reverse=True)[:10]
    for cwd, agg in top:
        label = cwd if len(cwd) <= 50 else "..." + cwd[-47:]
        print(f"  ${agg.cost_usd:>8,.2f}  {agg.turns:>4} turns  {label}")
    print()
    print(f"=== current session: {rpt.last_session_id[:8]}... ===")
    if rpt.last_session_id:
        cur = rpt.by_session[rpt.last_session_id]
        print(f"  cost:    ${cur.cost_usd:.4f}")
        print(f"  turns:   {cur.turns}")
        print(f"  last at: {rpt.last_session_at}")
