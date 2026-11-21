"""Parse Claude Code's local JSONL transcripts to compute usage and cost.

The background reader maintains a local derived index of transcripts under
~/.claude/projects/<encoded-path>/<session-uuid>.jsonl.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from functools import reduce
from operator import add
from pathlib import Path
from typing import Iterable

from jsonl_store import IndexStore
from paths import app_dir

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


@dataclass(slots=True)
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

    def _copy(self):
        # The UI and public callers own mutable snapshots. Only copy the final
        # buckets, never the transcript/turn index on the background thread.
        def buckets(source):
            return defaultdict(Aggregate, {k: Aggregate(**vars(v)) for k, v in source.items()})

        return UsageReport(today=Aggregate(**vars(self.today)),
                           this_month=Aggregate(**vars(self.this_month)),
                           by_project=buckets(self.by_project),
                           by_project_month=buckets(self.by_project_month),
                           by_session=buckets(self.by_session), last_session_id=self.last_session_id,
                           last_session_at=self.last_session_at, all_time=Aggregate(**vars(self.all_time)))


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


@dataclass(slots=True)
class _Run:
    """Consecutive winners with identical destinations in the original merge."""
    project: str
    session: str
    today: bool
    month: bool
    latest: datetime | None = None
    totals: Aggregate = field(default_factory=Aggregate)
    costs: list[float] = field(default_factory=list)

    def merge(self, into: Aggregate):
        # Floating point addition isn't associative. Start at the previous
        # file's exact accumulator; CPython 3.12+ sum uses compensated addition.
        t = self.totals
        if not into.turns:
            into.cost_usd = t.cost_usd
        elif sys.version_info < (3, 12):
            into.cost_usd = sum(self.costs, into.cost_usd)
        else:
            into.cost_usd = reduce(add, self.costs, into.cost_usd)
        into.input_tokens += t.input_tokens
        into.output_tokens += t.output_tokens
        into.cache_read_tokens += t.cache_read_tokens
        into.cache_5m_tokens += t.cache_5m_tokens
        into.cache_1h_tokens += t.cache_1h_tokens
        into.turns += t.turns


@dataclass(slots=True)
class _File:
    mtime: int
    size: int
    inode: int
    device: int
    guard: str
    offset: int                 # start of first unterminated line
    line_no: int                # ordinal at offset, including skipped lines
    tail_key: str | None        # valid JSON at EOF, provisional until newline
    turns: dict[str, TurnCost]
    runs: list[_Run] = field(default_factory=list)

    def pack(self):
        rows = [[key, t.timestamp.isoformat() if t.timestamp is not None else None,
                 t.project_cwd, t.session_id, t.model, t.input_tokens, t.output_tokens,
                 t.cache_read_tokens, t.cache_5m_tokens, t.cache_1h_tokens, t.cost_usd]
                for key, t in self.turns.items()]
        return [self.mtime, self.size, self.inode, self.device, self.guard, self.offset,
                self.line_no, self.tail_key, rows]

    @classmethod
    def unpack(cls, data):
        mtime, size, inode, dev, guard, offset, number, tail, rows = data
        strings = {}
        turns = {}
        for key, stamp, cwd, sid, model, *counts in rows:
            turns[key] = TurnCost(datetime.fromisoformat(stamp) if stamp else None,
                                  strings.setdefault(cwd, cwd), strings.setdefault(sid, sid),
                                  strings.setdefault(model, model), *counts)
        return cls(mtime, size, inode, dev, guard, offset, number, tail, turns)

    def include(self, turn, day, month):
        project = turn.project_cwd or "<unknown>"
        ts = turn.timestamp
        today = ts is not None and ts >= day
        monthly = ts is not None and ts >= month
        run = self.runs[-1] if self.runs else None
        if (run is None or run.project != project or run.session != turn.session_id
                or run.today != today or run.month != monthly):
            run = _Run(project, turn.session_id, today, monthly)
            self.runs.append(run)
        run.totals.add(turn)
        run.costs.append(turn.cost_usd)
        if ts is not None and (run.latest is None or ts > run.latest):
            run.latest = ts


def _guard(f, size):
    """Check the old head, middle and append boundary without hashing a large file."""
    digest = hashlib.blake2b(digest_size=16)
    for pos in (0, size // 2, max(0, size - 4096)):
        f.seek(pos)
        digest.update(f.read(min(4096, size - pos)))
    return digest.hexdigest()


class _ChangedDuringRead(OSError):
    pass


def _record(raw, filename, fallback, number, known, added, provisional, strings):
    # An escaped JSON member name may spell "usage" as e.g. "us\u0061ge".
    # Most transcript bytes are tool results with no usage member at any depth.
    if b'"usage"' not in raw and b'\\u' not in raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8").strip())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    msg = obj.get("message") or {}
    mid = msg.get("id")
    key = f"{mid}:{obj.get('requestId')}" if mid else f"@{filename}:{number}"
    if key in added or (key in known and key != provisional):
        return None
    turn = _parse_turn(obj, fallback)
    if turn is None:
        return None
    turn.project_cwd = strings.setdefault(turn.project_cwd, turn.project_cwd)
    turn.session_id = strings.setdefault(turn.session_id, turn.session_id)
    turn.model = strings.setdefault(turn.model, turn.model)
    added[key] = turn
    return key


def _lines(f, offset, size):
    """Yield the bytes and commit position of each logical text-mode line."""
    f.seek(offset)
    while offset < size:
        raw = f.readline(size - offset)
        if not raw:
            raise _ChangedDuringRead("transcript truncated during read")
        parts = raw.splitlines(keepends=True) if b"\r" in raw else (raw,)
        for part in parts:
            offset += len(part)
            # Retain a terminal CR as well: it may become a CRLF next time.
            complete = part.endswith(b"\n") or (offset < size and part.endswith(b"\r"))
            yield part, offset if complete else None


def _parse_file(path, previous):
    """Stage a bounded read. Commit changes only after the entire slice was read."""
    with path.open("rb") as f:
        st = os.fstat(f.fileno())
        size = st.st_size
        append = (previous is not None and size > previous.size
                  and (st.st_ino, st.st_dev) == (previous.inode, previous.device)
                  and _guard(f, previous.size) == previous.guard)
        offset = previous.offset if append else 0
        number = previous.line_no if append else 0
        provisional = previous.tail_key if append else None
        known = previous.turns if append else {}
        added = {}
        strings = {}
        tail = None
        guard = _guard(f, size)
        filename, fallback = path.name, path.stem
        for raw, commit in _lines(f, offset, size):
            key = _record(raw, filename, fallback, number, known, added, provisional, strings)
            if commit is None:
                tail = key
            else:
                offset = commit
                number += 1
        end = os.fstat(f.fileno())
        if end.st_size < size or (end.st_size == size and end.st_mtime_ns != st.st_mtime_ns):
            raise _ChangedDuringRead("transcript changed during read")
    if provisional is not None:
        del known[provisional]
    known.update(added)
    file = _File(st.st_mtime_ns, size, st.st_ino, st.st_dev, guard, offset, number,
                 tail, known, previous.runs if append else [])
    rebuild = previous is not None and (not append or provisional is not None)
    return file, added, rebuild


class _ReportCache:
    AUDIT_SECONDS = 600
    SAVE_SECONDS = 300

    def __init__(self, root, database=None):
        self.root = root
        self.store = IndexStore(database, root, PRICING) if database else None
        self.files = self.store.load(_File.unpack) if self.store else {}
        self.owners = {}
        self.dirty = {}
        self.retry = set()
        self.watch = None
        self.root_id = None
        self.directories = set()
        self.next_audit = self.next_save = 0.0
        self.window = self.report = None

    def close(self):
        if self.watch is not None:
            self.watch.close()
            self.watch = None

    def _changes(self):
        clock = time.monotonic()
        audit = clock >= self.next_audit
        try:
            st = self.root.stat()
            identity = st.st_dev, st.st_ino
        except OSError:
            identity = None
        if identity != self.root_id or identity is None:
            self.close()
            self.root_id = identity
            audit = True
        paths = None
        if identity is not None and os.name == "nt":
            try:
                if self.watch is None and audit:
                    from jsonl_watch import DirectoryWatch
                    self.watch = DirectoryWatch(self.root)
                if self.watch is not None:
                    paths = self.watch.changes()
            except (ImportError, OSError):
                self.close()
        if paths is not None and not audit:
            paths = {self.root / p for p in paths}
            if not any(p in self.directories or p.is_dir() for p in paths):
                return paths | self.retry, ()
        # Start/arm the watch BEFORE enumeration to capture concurrent writes.
        if audit:
            self.next_audit = clock + self.AUDIT_SECONDS
        paths = set()
        self.directories.clear()
        for dirname, _, names in os.walk(self.root):
            parent = Path(dirname)
            self.directories.add(parent)
            paths.update(parent / name for name in names if
                         (name.lower() if os.name == "nt" else name).endswith(".jsonl"))
        current_names = {str(p) for p in paths}
        removed = [p for p in self.files if str(p) not in current_names]
        self.retry.intersection_update(paths)
        return paths, removed

    def _refresh_cache(self, rebuild):
        """Return newly parsed keys, or signal that the winning order needs replay."""
        additions = {}
        changes, removed = self._changes()
        for path in removed:
            rebuild |= bool(self.files.pop(path).turns)
            self.dirty[str(path)] = None
        for path in changes:
            previous = self.files.get(path)
            try:
                st = path.stat()
                if previous and path not in self.retry:
                    signature = st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev
                    if signature == (previous.mtime, previous.size, previous.inode, previous.device):
                        continue
                entry, added, replaced = _parse_file(path, previous)
                self.files[path] = entry
                rebuild |= replaced
                if not rebuild and added:
                    additions[path] = added
                self.retry.discard(path)
            except _ChangedDuringRead:
                # A bounded slice was lost mid-read. Keep the last coherent
                # snapshot and retry even if no further notification arrives.
                self.retry.add(path)
                continue
            except OSError as exc:
                if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
                    self.retry.discard(path)
                else:
                    self.retry.add(path)
                if previous is None:
                    continue
                del self.files[path]
                rebuild |= bool(previous.turns)
            self.dirty[str(path)] = self.files.get(path)
        return rebuild, additions

    def _aggregate(self, day, month, rebuild, additions):
        changed = rebuild
        if not rebuild:
            for path in sorted(additions):
                for key, turn in additions[path].items():
                    owner = self.owners.get(key)
                    if owner is None:
                        self.owners[key] = path
                        self.files[path].include(turn, day, month)
                        changed = True
                    elif path < owner:
                        rebuild = changed = True
                        break
                if rebuild:
                    break
        if rebuild:
            # Deletion/rewrite can expose a previously losing copy. Calendar
            # changes are also rare, and use these cached turns without any I/O.
            self.owners.clear()
            for path in sorted(self.files):
                file = self.files[path]
                file.runs.clear()
                for key, turn in file.turns.items():
                    if key not in self.owners:
                        self.owners[key] = path
                        file.include(turn, day, month)
            self.window = day, month
        if changed:
            rpt = UsageReport()
            for path in sorted(self.files):
                for run in self.files[path].runs:
                    run.merge(rpt.all_time)
                    run.merge(rpt.by_project[run.project])
                    run.merge(rpt.by_session[run.session])
                    if run.today:
                        run.merge(rpt.today)
                    if run.month:
                        run.merge(rpt.this_month)
                        run.merge(rpt.by_project_month[run.project])
                    if run.latest is not None and (rpt.last_session_at is None
                                                   or run.latest > rpt.last_session_at):
                        rpt.last_session_at, rpt.last_session_id = run.latest, run.session
            self.report = rpt

    def _checkpoint(self):
        if self.store and self.dirty and time.monotonic() >= self.next_save:
            if self.store.save(self.files, self.dirty):
                self.dirty.clear()
            self.next_save = time.monotonic() + self.SAVE_SECONDS

    def build(self, now):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        try:
            rebuild, added = self._refresh_cache(self.window != (day, month) or self.report is None)
            self._aggregate(day, month, rebuild, added)
            self._checkpoint()
            return self.report
        except Exception:
            # A failed cycle may have drained notifications or staged some files.
            self.report = None
            self.next_audit = 0.0
            raise


_cache = None
_lock = threading.RLock()
_database = app_dir() / "jsonl_index.sqlite3"


def _close_cache():
    global _cache
    with _lock:
        if _cache is not None:
            _cache.close()
            _cache = None


@atexit.register
def _release_watch():
    # The daemon may still be building its first report when the UI exits.
    if _lock.acquire(blocking=False):
        try:
            _close_cache()
        finally:
            _lock.release()


def _for_root(root):
    global _cache
    root = Path(root).absolute()
    if _cache is None or root != _cache.root:
        _close_cache()
        _cache = _ReportCache(root, _database if root == PROJECTS_ROOT else None)
    return _cache


def build_report(root: Path = PROJECTS_ROOT, now: datetime | None = None) -> UsageReport:
    """Return an independent report snapshot using the original local-time boundaries."""
    with _lock:
        cache = _for_root(root)
        return cache.build(now if now is not None else datetime.now().astimezone())._copy()


def iter_turns(root: Path = PROJECTS_ROOT) -> Iterable[TurnCost]:
    """Yield global first winners in Path sort order, including cross-file copies."""
    with _lock:
        cache = _for_root(root)
        # Iteration has no calendar or timezone requirements (e.g. naive stamps).
        cache.report = None
        cache._refresh_cache(True)
        winners = {}
        for path in sorted(cache.files):
            for key, turn in cache.files[path].turns.items():
                winners.setdefault(key, turn)
        turns = winners.values()
    yield from turns


if __name__ == "__main__":
    # Smoke test: build the report, print summary.
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
