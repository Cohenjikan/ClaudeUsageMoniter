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
    timestamp: datetime | None    # When generated (UTC); None if the record's timestamp was unparseable.
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
    # Same shape as by_project but restricted to turns >= local month-start — this
    # is what the floating window's "Top projects (this month)" panel needs.
    by_project_month: dict[str, Aggregate] = field(default_factory=lambda: defaultdict(Aggregate))
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

    # Timestamp — JSONL uses ISO 8601 with "Z" suffix. On parse failure we keep
    # None rather than substituting now(): a bogus now() timestamp would silently
    # pollute the today/this_month buckets (build_report skips None-ts turns from
    # those buckets while still counting them toward all_time/by_project/by_session).
    ts_str = obj.get("timestamp") or ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        ts = None

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


# ---- Incremental per-file parse cache ----
# build_report runs every JSONL_INTERVAL_SEC (30s) and the projects dir is ~150MB
# across ~117 files; re-reading and re-parsing all of it each tick burned ~1s of
# CPU under the GIL and contributed to UI stutter. Almost nothing changes between
# ticks (one active session file grows, the rest are frozen history), so we cache
# the parsed result of each file keyed by its (st_mtime_ns, st_size) and only
# reparse files whose stat changed.
#
# Map: absolute path str -> (st_mtime_ns, st_size, {dedup_key: TurnCost}).
# We deliberately store only the parsed TurnCosts (≈23k objects total after
# dedup), never the raw lines, so memory stays bounded.
_file_cache: dict[str, tuple[int, int, dict[str, "TurnCost"]]] = {}


def _parse_file(path: Path) -> dict[str, TurnCost]:
    """Parse one JSONL file into {dedup_key: TurnCost}, first-wins WITHIN the file.

    Dedup key is ``f"{message.id}:{requestId}"`` for records that carry a
    message.id (see the de-dup rationale below). Records lacking a message.id
    get a per-file-unique key ``f"@{filename}:{line_no}"`` so they are NEVER
    deduped (preserving the original never-drop-unkeyed behavior) and can never
    collide with a real (message.id, requestId) key or with another file's keys.

    Returns {} on any read error (matches the old per-file skip-on-error path).
    """
    fallback_sid = path.stem
    fname = path.name
    out: dict[str, TurnCost] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line_no, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turn = _parse_turn(obj, fallback_sid)
                if turn is None:
                    continue
                msg = obj.get("message") or {}
                mid = msg.get("id")
                if mid:
                    # requestId lives at the top level of the record.
                    key = f"{mid}:{obj.get('requestId')}"
                else:
                    # Unkeyed record — never deduped, file-locally unique.
                    key = f"@{fname}:{line_no}"
                # First-wins within the file: identical (mid,rid) lines carry the
                # same usage, so keeping the first is correct and stable.
                if key not in out:
                    out[key] = turn
    except (PermissionError, OSError):
        return {}
    return out


def _refresh_cache(root: Path) -> list[str]:
    """Sync _file_cache with the current set of *.jsonl files under root.

    Reuses cache entries whose (mtime_ns, size) are unchanged, reparses changed
    or new files, and evicts entries for files that have disappeared. Returns the
    sorted list of current absolute paths (the merge order build_report uses).
    """
    if not root.exists():
        _file_cache.clear()
        return []
    current: list[str] = []
    for session_file in sorted(root.rglob("*.jsonl")):
        key = str(session_file)
        current.append(key)
        try:
            st = session_file.stat()
        except OSError:
            # Vanished between rglob and stat — treat as absent.
            current.pop()
            continue
        sig = (st.st_mtime_ns, st.st_size)
        cached = _file_cache.get(key)
        if cached is not None and (cached[0], cached[1]) == sig:
            continue  # unchanged — reuse parsed turns
        _file_cache[key] = (sig[0], sig[1], _parse_file(session_file))
    # Evict files that no longer exist.
    live = set(current)
    for stale in [k for k in _file_cache if k not in live]:
        del _file_cache[stale]
    return current


def iter_turns(root: Path = PROJECTS_ROOT) -> Iterable[TurnCost]:
    """Yield each billable TurnCost once across all session files under root.

    CRITICAL — de-duplication: Claude Code writes a SEPARATE JSONL line for
    every content block of an assistant message (thinking, text, and each
    tool_use), and EVERY one of those lines carries the *same* ``message.id``,
    ``requestId``, and an identical ``usage`` object (usage is per-API-response,
    not per-block). A single Opus turn with 4 tool calls therefore appears as
    5 lines with identical usage. Summing every line over-counts cost massively
    — on real data here, 62% of usage-bearing lines were such duplicates,
    inflating the monthly total ~4.6x ($20k -> $4.5k).

    The same ``(message.id, requestId)`` pair also reappears when session
    history is copied into a resumed/forked/compacted session file, so the
    de-dup set is kept GLOBAL across all files, not per-file.

    We therefore count each ``(message.id, requestId)`` exactly once. Records
    with no ``message.id`` are never dropped — they're treated as unique.

    Now backed by the incremental per-file cache: yields turns in sorted-path
    order, applying the global first-wins dedup across files.
    """
    paths = _refresh_cache(root)
    seen_keys: set[str] = set()
    for key in paths:
        entry = _file_cache.get(key)
        if entry is None:
            continue
        for dedup_key, turn in entry[2].items():
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            yield turn


def build_report(root: Path = PROJECTS_ROOT, now: datetime | None = None) -> UsageReport:
    """Aggregate all turns into the structured report the UI consumes.

    Day / month boundaries follow the user's LOCAL timezone, not UTC — the
    user thinks in local-calendar terms ("today's spend"), not in UTC days.
    JSONL timestamps come in as UTC-aware datetimes; Python compares aware
    datetimes by absolute moment, so the cross-tz comparison is correct.

    Turns whose timestamp failed to parse (TurnCost.timestamp is None) still
    count toward all_time / by_project / by_session, but are excluded from
    today / this_month / by_project_month and from the last_session_at race,
    so an unparseable timestamp can't pollute calendar-scoped totals.

    Backed by the incremental per-file cache (see _refresh_cache / _parse_file):
    only changed files are reparsed between calls.
    """
    if now is None:
        # `astimezone()` with no argument attaches the system's local tz to the
        # current moment, giving us a local-aware datetime we can floor to
        # local midnight / month-start.
        now = datetime.now().astimezone()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Refresh the cache once, then merge globally first-wins in sorted-path order.
    paths = _refresh_cache(root)
    rpt = UsageReport()
    seen_keys: set[str] = set()
    for key in paths:
        entry = _file_cache.get(key)
        if entry is None:
            continue
        for dedup_key, turn in entry[2].items():
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            rpt.all_time.add(turn)
            rpt.by_project[turn.project_cwd or "<unknown>"].add(turn)
            rpt.by_session[turn.session_id].add(turn)
            ts = turn.timestamp
            if ts is not None:
                if ts >= today_start:
                    rpt.today.add(turn)
                if ts >= month_start:
                    rpt.this_month.add(turn)
                    rpt.by_project_month[turn.project_cwd or "<unknown>"].add(turn)
                if rpt.last_session_at is None or ts > rpt.last_session_at:
                    rpt.last_session_at = ts
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
