import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import jsonl_costs as costs


NOW = datetime(2026, 11, 30, 23, tzinfo=timezone(timedelta(hours=11)))


def row(mid="a", tokens=1, stamp="2026-11-30T00:00:00Z", sid="s", cwd="p"):
    return json.dumps({"timestamp": stamp, "sessionId": sid, "cwd": cwd,
                       "message": {"id": mid, "model": "sonnet-4", "usage": {
                           "output_tokens": tokens, "cache_read_input_tokens": 13}}}).encode()


class JsonlTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "projects"
        self.root.mkdir()
        self.db = self.base / "index.sqlite3"
        self.file = self.root / "b.jsonl"
        self.cache = costs._ReportCache(self.root, self.db)
        self.addCleanup(lambda: self.cache.close())

    def report(self, now=NOW):
        return self.cache.build(now)

    def restart(self):
        self.cache.close()
        self.cache = costs._ReportCache(self.root, self.db)

    def append(self, data):
        with self.file.open("ab") as f:
            f.write(data)

    def test_append_reads_from_committed_line_and_keeps_old_snapshot(self):
        self.file.write_bytes(row() + b"\r\n\n" + row("b")[:30])
        old = self.report()
        offset = len(row()) + 3
        self.assertEqual(self.cache.files[self.file].offset, offset)
        self.append(row("b")[30:] + b"\n" + row("b", 999) + b"\n")
        with patch.object(costs, "_lines", wraps=costs._lines) as lines:
            rpt = self.report()
            self.assertEqual(lines.call_args.args[1], offset)
        self.assertEqual(rpt.all_time.turns, 2)
        self.assertEqual(rpt.by_session["s"].output_tokens, 2)
        self.assertEqual(old.all_time.turns, 1)

    def test_valid_unterminated_json_is_provisional_even_after_restart(self):
        self.file.write_bytes(row())
        self.assertEqual(self.report().all_time.turns, 1)
        self.restart()
        self.append(b"x\n" + row("a", 7) + b"\n")
        self.assertEqual(self.report().all_time.output_tokens, 7)

    def test_concurrent_truncation_retries_without_a_second_notification(self):
        self.file.write_bytes(row() + b"\n")
        self.report()
        self.append(row("b") + b"\n")
        with patch.object(costs, "_parse_file", side_effect=costs._ChangedDuringRead):
            self.assertEqual(self.report().all_time.turns, 1)
        self.assertIn(self.file, self.cache.retry)
        self.assertEqual(self.report().all_time.turns, 2)

    def test_escaped_usage_and_partial_utf8(self):
        line = row(cwd="X").replace(b'"X"', '"東京"'.encode()).replace(b'"usage"', b'"us\\u0061ge"')
        split = line.index("東".encode()) + 1
        self.file.write_bytes(line[:split])
        self.assertEqual(self.report().all_time.turns, 0)
        self.append(line[split:] + b"\n")
        self.assertEqual(self.report().by_project["東京"].turns, 1)

    def test_public_snapshots_are_independent_and_iterator_accepts_naive_time(self):
        self.addCleanup(costs._close_cache)
        self.file.write_bytes(row())
        snapshot = costs.build_report(self.root, NOW)
        snapshot.by_project["p"].turns = 0
        snapshot.today.cost_usd = -1
        self.assertEqual(costs.build_report(self.root, NOW), self.report())
        self.file.write_bytes(row(stamp="2026-11-30T01:02:03"))
        self.assertIsNone(next(costs.iter_turns(self.root)).timestamp.tzinfo)

    def test_universal_newlines_and_legacy_unkeyed_basename_collision(self):
        other = self.root / "z" / self.file.name
        other.parent.mkdir()
        self.file.write_bytes(b"bad\r\r" + row(None, 5) + b"\r")
        other.write_bytes(b"\n\n" + row(None, 999) + b"\n")
        self.assertEqual(self.report().all_time.output_tokens, 5)
        self.append(b"\n" + row(None, 11) + b"\r" + row(None, 17))
        self.assertEqual(self.report().all_time.output_tokens, 33)
        self.assertEqual(list(self.cache.files[self.file].turns),
                         ["@b.jsonl:2", "@b.jsonl:3", "@b.jsonl:4"])

    def test_earlier_duplicate_displaces_owner_and_tied_latest(self):
        early = self.root / "a.jsonl"
        self.file.write_bytes(row() + b"\n" + row("b", sid="later") + b"\n")
        early.write_bytes(row("old", stamp="invalid") + b"\n")
        self.assertEqual(self.report().last_session_id, "s")
        with early.open("ab") as f:
            f.write(row(tokens=90, sid="early", cwd="other") + b"\n")
        rpt = self.report()
        self.assertEqual(rpt.all_time.output_tokens, 92)
        self.assertEqual(rpt.last_session_id, "early")
        self.assertEqual(list(rpt.by_project_month), ["other", "p"])
        early.unlink()
        self.assertEqual(self.report().all_time.output_tokens, 2)
        self.assertEqual(self.report().last_session_id, "s")

    def test_day_month_and_future_turns_change_without_disk_io(self):
        self.file.write_bytes(b"\n".join([row(), row("old", stamp=None),
                                         row("later", stamp="2026-11-30T14:00:00Z")]))
        self.assertEqual(self.report().today.turns, 2)
        with patch.object(costs, "_parse_file", side_effect=AssertionError):
            later = self.report(NOW + timedelta(hours=2))
        self.assertEqual(later.all_time.turns, 3)
        self.assertEqual(later.today.turns, 1)
        self.assertEqual(later.by_project_month["p"].turns, 1)
        self.assertEqual(self.report().today.turns, 2)

    def test_replacement_and_larger_rewrite_with_same_first_block(self):
        prefix = b" " * 9000 + b"\n"
        self.file.write_bytes(prefix + row())
        self.report()
        self.file.write_bytes(prefix + row(tokens=456) + b"\n" + row("b"))
        self.assertEqual(self.report().all_time.output_tokens, 457)
        other = self.root / "replacement"
        other.write_bytes(row(tokens=20))
        other.replace(self.file)
        self.assertEqual(self.report().all_time.output_tokens, 20)

    def test_float_order_for_append_in_early_file(self):
        after = self.root / "z.jsonl"
        self.file.write_bytes(row(tokens=10**15) + b"\n")
        after.write_bytes(b"\n".join(row(f"x{i}", i + 5) for i in range(200)))
        self.report()
        self.append(row("insert", 80) + b"\n")
        rpt = self.report()
        expected = 0.0
        for path in (self.file, after):
            for line in path.read_bytes().splitlines():
                expected += costs._parse_turn(json.loads(line), "").cost_usd
        for agg in (rpt.all_time, rpt.today, rpt.this_month, rpt.by_project["p"],
                    rpt.by_project_month["p"], rpt.by_session["s"]):
            self.assertEqual(agg.cost_usd, expected)

    def test_disk_checkpoint_throttle_corruption_and_version(self):
        self.file.write_bytes(row() + b"\n")
        self.report()
        self.restart()
        with patch.object(costs, "_parse_file", side_effect=AssertionError):
            self.assertEqual(self.report().all_time.turns, 1)
        self.append(row("b") + b"\n")
        self.report()
        with patch.object(self.cache.store, "save", side_effect=AssertionError):
            self.append(row("c") + b"\n")
            self.report()
        self.restart()
        self.assertEqual(self.report().all_time.turns, 3)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE meta SET identity='old'")
        db.close()
        self.restart()
        self.assertFalse(self.cache.files)
        self.report()
        self.db.write_bytes(b"damaged")
        self.restart()
        self.assertEqual(self.report().all_time.turns, 3)
        self.restart()
        self.assertTrue(self.cache.files)

    def test_failed_atomic_replacement_keeps_prior_index_and_retries(self):
        self.db.write_bytes(b"old incompatible index")
        self.file.write_bytes(row())
        self.restart()
        with patch("jsonl_store.os.replace", side_effect=PermissionError):
            self.assertEqual(self.report().all_time.turns, 1)
        self.assertEqual(self.db.read_bytes(), b"old incompatible index")
        self.assertFalse(list(self.base.glob("*.tmp")))
        self.cache.next_save = 0
        self.report()
        self.restart()
        self.assertTrue(self.cache.files)

    @unittest.skipUnless(os.name == "nt", "case-insensitive Windows paths")
    def test_case_only_rename_changes_fallback_identifiers(self):
        self.file.write_bytes(row(None, sid=None))
        self.assertEqual(self.report().last_session_id, "b")
        self.file.rename(self.root / "B.jsonl")
        self.cache.next_save = 0
        self.assertEqual(self.report().last_session_id, "B")
        self.restart()
        with patch.object(costs, "_parse_file", side_effect=AssertionError):
            self.assertEqual(self.report().last_session_id, "B")

    def test_event_overflow_and_missing_notifications_reconcile(self):
        self.report()
        self.cache.close()
        self.cache.watch = Mock()
        self.cache.watch.changes.return_value = None  # overflow
        self.file.write_bytes(row() + b"\n")
        self.assertEqual(self.report().all_time.turns, 1)
        self.cache.watch.changes.return_value = set()
        self.file.unlink()
        self.cache.next_audit = 0
        self.assertEqual(self.report().all_time.turns, 0)

    @unittest.skipUnless(os.name == "nt", "requires Windows directory notifications")
    def test_watcher_idle_directory_move_and_fallback(self):
        initial = self.report()
        self.assertIsNotNone(self.cache.watch)
        with patch.object(os, "walk", side_effect=AssertionError):
            self.assertIs(self.report(), initial)
        external = self.base / "incoming"
        external.mkdir()
        (external / "nested.jsonl").write_bytes(row() + b"\n")
        external.rename(self.root / "sub")
        self.assertEqual(self.report().all_time.turns, 1)
        (self.root / "sub").rename(external)
        self.cache.watch.changes = Mock(side_effect=OSError("unsupported"))
        self.assertEqual(self.report().all_time.turns, 0)
        self.file.write_bytes(row() + b"\n")
        self.assertEqual(self.report().all_time.turns, 1)
