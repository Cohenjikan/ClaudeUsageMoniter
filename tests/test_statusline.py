"""Tests for the statusLine bridge data path. Stdlib unittest only:

    python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import statusline  # noqa: E402
from usage_api import snapshot_from_body  # noqa: E402

PAYLOAD = {
    "session_id": "abc",
    "transcript_path": "/secret/path.jsonl",
    "cwd": "/home/me/project",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
    "version": "2.1.120",
    "rate_limits": {
        "five_hour": {"used_percentage": 33.4, "resets_at": 1767225600},
        "seven_day": {"used_percentage": 91, "resets_at": 1767600000},
    },
}


class CaptureTests(unittest.TestCase):
    def test_make_capture_keeps_only_safe_fields(self):
        rec = statusline.make_capture(PAYLOAD, now=1000.0)
        self.assertEqual(rec["captured_at"], 1000.0)
        self.assertEqual(rec["model"], "Opus")
        self.assertEqual(rec["claude_code_version"], "2.1.120")
        dumped = json.dumps(rec)
        self.assertNotIn("/secret/path.jsonl", dumped)
        self.assertNotIn("/home/me/project", dumped)

    def test_no_rate_limits_means_no_capture(self):
        self.assertIsNone(statusline.make_capture({"model": {}}))
        self.assertIsNone(statusline.make_capture({"rate_limits": {}}))
        self.assertIsNone(statusline.make_capture({"rate_limits": {"five_hour": {}}}))
        self.assertIsNone(statusline.make_capture("nope"))

    def test_body_maps_to_usage_snapshot(self):
        rec = statusline.make_capture(PAYLOAD, now=1000.0)
        body = statusline.body_from_capture(rec)
        snap = snapshot_from_body(body, 1000.0)
        self.assertAlmostEqual(snap.five_hour_pct, 33.4)
        self.assertEqual(snap.seven_day_pct, 91.0)
        self.assertTrue(snap.five_hour_reset.startswith("2026-01-01T00:00:00"))
        self.assertTrue(snap.five_hour_active)
        self.assertEqual(snap.scoped_limits, ())

    def test_reset_formats(self):
        self.assertEqual(statusline._iso_reset(1767225600000),
                         statusline._iso_reset(1767225600))
        self.assertEqual(statusline._iso_reset("2026-01-01T00:00:00Z"),
                         "2026-01-01T00:00:00Z")
        self.assertEqual(statusline._iso_reset("1767225600"),
                         statusline._iso_reset(1767225600))
        self.assertEqual(statusline._iso_reset(None), "")
        self.assertEqual(statusline._iso_reset("garbage"), "")

    def test_unknown_window_becomes_scoped_bucket(self):
        rec = {"captured_at": 1.0, "rate_limits": {
            "five_hour": {"used_percentage": 10},
            "seven_day_fable": {"used_percentage": 55, "display_name": "Fable",
                                "resets_at": 1767600000},
        }}
        snap = snapshot_from_body(statusline.body_from_capture(rec), 1.0)
        self.assertEqual(len(snap.scoped_limits), 1)
        self.assertEqual(snap.scoped_limits[0].name, "Fable")
        self.assertEqual(snap.scoped_limits[0].pct, 55.0)
        # Missing resets_at => inactive window, not a crash.
        self.assertFalse(snap.seven_day_active)

    def test_write_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "cap.json"
            rec = statusline.make_capture(PAYLOAD, now=5.0)
            statusline.write_capture(rec, p)
            self.assertEqual(statusline.read_capture(p), rec)
            p.write_text("{broken", encoding="utf-8")
            self.assertIsNone(statusline.read_capture(p))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = self.dir / "settings.json"
        self.chain = self.dir / "usage-monitor" / "prev.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_install_fresh_and_uninstall(self):
        statusline.install(self.settings, self.chain, command='"py" "statusline_bridge.py"')
        self.assertTrue(statusline.is_installed(self.settings))
        self.assertEqual(self._read()["statusLine"]["type"], "command")
        statusline.uninstall(self.settings, self.chain)
        self.assertNotIn("statusLine", self._read())
        self.assertFalse(statusline.is_installed(self.settings))

    def test_install_preserves_other_settings_and_chains_previous(self):
        prev = {"type": "command", "command": "~/.claude/my-line.sh", "padding": 1}
        self.settings.write_text(json.dumps({"model": "opus", "statusLine": prev}),
                                 encoding="utf-8")
        statusline.install(self.settings, self.chain, command='"x" "statusline_bridge.py"')
        data = self._read()
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["statusLine"]["padding"], 1)
        self.assertEqual(statusline.load_previous(self.chain), prev)
        # Re-install must not overwrite the chain with our own command.
        statusline.install(self.settings, self.chain, command='"y" "statusline_bridge.py"')
        self.assertEqual(statusline.load_previous(self.chain), prev)
        self.assertTrue((self.dir / "settings.json.bak-usage-monitor").exists())
        statusline.uninstall(self.settings, self.chain)
        self.assertEqual(self._read()["statusLine"], prev)
        self.assertFalse(self.chain.exists())

    def test_uninstall_leaves_foreign_statusline_alone(self):
        foreign = {"type": "command", "command": "ccusage statusline"}
        self.settings.write_text(json.dumps({"statusLine": foreign}), encoding="utf-8")
        statusline.uninstall(self.settings, self.chain)
        self.assertEqual(self._read()["statusLine"], foreign)

    def test_invalid_settings_json_is_never_clobbered(self):
        self.settings.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(statusline.SettingsFileError):
            statusline.install(self.settings, self.chain, command="statusline_bridge")
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{ not json")

    def test_bridge_command_uses_forward_slashes(self):
        cmd = statusline.bridge_command()
        self.assertNotIn("\\", cmd)
        self.assertTrue(statusline.is_bridge_command(cmd))


class BridgeScriptTests(unittest.TestCase):
    """Run statusline_bridge.py as Claude Code would: JSON on stdin."""

    def _run(self, payload, home: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        return subprocess.run(
            [sys.executable, str(ROOT / "statusline_bridge.py")],
            input=json.dumps(payload).encode() if not isinstance(payload, bytes) else payload,
            capture_output=True, env=env, timeout=20, cwd=str(home),
        )

    def test_writes_capture_and_prints_default_line(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            proc = self._run(PAYLOAD, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.decode("utf-8").strip(), "Opus · 5h 33% · 7d 91%")
            cap = json.loads((home / ".claude" / "usage-monitor" / "statusline.json")
                             .read_text(encoding="utf-8"))
            self.assertEqual(cap["rate_limits"], PAYLOAD["rate_limits"])
            self.assertLessEqual(abs(cap["captured_at"] - time.time()), 60)

    def test_garbage_input_never_fails(self):
        with tempfile.TemporaryDirectory() as d:
            proc = self._run(b"\xff not json", Path(d))
            self.assertEqual(proc.returncode, 0)
            self.assertFalse((Path(d) / ".claude" / "usage-monitor" / "statusline.json").exists())

    @unittest.skipIf(os.name == "nt", "chain test uses /bin/sh")
    def test_chains_previous_statusline(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            chain = home / ".claude" / "usage-monitor" / "previous-statusline.json"
            chain.parent.mkdir(parents=True)
            chain.write_text(json.dumps({"previous": {
                "type": "command",
                "command": "python3 -c \"import sys,json;print('PREV', json.load(sys.stdin)['model']['display_name'])\"",
            }}), encoding="utf-8")
            proc = self._run(PAYLOAD, home)
            self.assertEqual(proc.stdout.decode().strip(), "PREV Opus")
            self.assertTrue((home / ".claude" / "usage-monitor" / "statusline.json").exists())


if __name__ == "__main__":
    unittest.main()
