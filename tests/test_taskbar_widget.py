"""Fullscreen regressions. Exercise the widget's handlers without creating a window."""
import importlib.util
import sys
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32" and importlib.util.find_spec("PIL"),
                     "requires Windows and Pillow")
class TaskbarWidgetTests(unittest.TestCase):
    def setUp(self):
        import taskbar_widget as m
        self.m = m
        with patch.object(m.NativeTaskbarWidget, "_run", lambda w: w._ready.set()):
            self.widget = m.NativeTaskbarWidget(side="right")
        self.widget.hwnd, self.widget._taskbar = 1, 2
        self.widget._uploaded_version = -1
        self.widget._event_proc = Mock()
        self.widget._upload = Mock(return_value=True)

        dll = patch.object(m, "u32")
        self.u = u = dll.start()
        self.addCleanup(dll.stop)
        dwm = patch.object(m, "dwm")
        self.dwm = dwm.start()
        self.addCleanup(dwm.stop)
        u.FindWindowW.return_value = 2
        u.FindWindowExW.return_value = 0
        u.GetForegroundWindow.return_value = 3
        u.GetDesktopWindow.return_value = 99
        u.GetAncestor.side_effect = lambda hwnd, _: hwnd
        u.GetWindowThreadProcessId.side_effect = lambda hwnd, _: hwnd
        self.minimized = set()
        u.IsIconic.side_effect = self.minimized.__contains__
        self.visible = False
        self.hidden = set()
        u.IsWindowVisible.side_effect = lambda hwnd: self.visible if hwnd == 1 else hwnd not in self.hidden
        self.monitor = m.wintypes.RECT(0, 0, 1920, 1080)
        self.bar = m.wintypes.RECT(0, 1032, 1920, 1080)
        self.video = m.wintypes.RECT(0, 0, 1920, 1080)
        self.rects = {1: m.wintypes.RECT(1808, 1046, 1908, 1066), 2: self.bar,
                      3: self.video, 4: m.wintypes.RECT(10, 10, 310, 210)}
        self.frames = {}
        self.cloaked = set()
        self.stack = [1, 3, 2]  # top to bottom
        u.IsWindow.side_effect = lambda hwnd: hwnd in self.stack
        self.owner = 2
        self.posts = []

        def get_window(hwnd, relation):
            if relation == m.GW_OWNER:
                return self.owner
            self.assertEqual(relation, m.GW_HWNDPREV)
            if hwnd not in self.stack:
                return 0
            index = self.stack.index(hwnd)
            return self.stack[index - 1] if index else 0

        def monitor_info(handle, ptr):
            ptr._obj.rcMonitor = self.monitor
            return True

        def write_rect(ptr, rect):
            m.ctypes.memmove(ptr, m.ctypes.byref(rect), m.ctypes.sizeof(rect))
            return True

        def attribute(hwnd, attr, ptr, size):
            if attr == m.DWMWA_CLOAKED:
                ptr._obj.value = int(hwnd in self.cloaked)
            else:
                self.assertEqual(attr, m.DWMWA_EXTENDED_FRAME_BOUNDS)
                write_rect(ptr, self.frames.get(hwnd, self.rects[hwnd]))
            return 0

        def post(*msg):
            self.posts.append(msg)
            return True

        def place(hwnd, after, x, y, w, h, flags):
            required = m.SWP_NOZORDER | m.SWP_NOOWNERZORDER | m.SWP_NOACTIVATE
            self.assertEqual(flags & required, required)
            if flags & m.SWP_SHOWWINDOW:
                self.visible = True
            if flags & m.SWP_HIDEWINDOW:
                self.visible = False

        u.GetMonitorInfoW.side_effect = monitor_info
        u.GetWindow.side_effect = get_window
        u.GetWindowRect.side_effect = lambda hwnd, ptr: write_rect(ptr, self.rects[hwnd])
        self.dwm.DwmGetWindowAttribute.side_effect = attribute
        u.SetWindowPos.side_effect = place
        u.PostMessageW.side_effect = post
        self.widget.update(m.Image.new("RGBA", (100, 20)))

    def drain(self):
        for _ in range(16):
            if not self.posts:
                return
            self.widget._proc(*self.posts.pop(0))
        self.fail("widget keeps posting updates")

    def event(self, event, hwnd, obj=0):
        # Deliver only if the real registration range and thread would match.
        for (first, last, tid), hook in self.widget._hooks.items():
            if first <= event <= last and tid in (0, hwnd):
                self.widget._on_win_event(hook, event, hwnd, obj, 0, hwnd, 0)
                break
        self.drain()

    def test_esc_same_hwnd_and_demoted_taskbar(self):
        m, w, u = self.m, self.widget, self.u
        u.GetWindowLongPtrW.return_value = 0  # Explorer never re-promotes the bar
        self.video.bottom = 980
        self.drain()
        self.assertTrue(self.visible)
        self.video.bottom = 1080
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 3)
        self.assertFalse(self.visible)
        w.show()
        w.update(m.Image.new("RGBA", (100, 20)))
        self.drain()
        self.assertFalse(self.visible)
        self.assertEqual(w._upload.call_count, 1)

        self.video.bottom = 980
        u.PostMessageW.reset_mock()
        for _ in range(2):
            w._on_win_event(1, m.EVENT_OBJECT_LOCATIONCHANGE, 3, 0, 0, 3, 0)
        u.PostMessageW.assert_called_once_with(1, m.WM_APP_UPDATE, 0, 0)
        self.drain()
        self.assertTrue(self.visible)
        self.assertEqual(w._upload.call_count, 2)
        u.SetWindowPos.reset_mock()
        w._apply()
        u.SetWindowPos.assert_not_called()
        self.assertEqual(w._upload.call_count, 2)
        u.SetTimer.assert_not_called()
        u.ShowWindow.assert_not_called()

    def test_secondary_monitor_minimize_auto_hide_and_user_visibility(self):
        w = self.widget
        self.video.left, self.video.right = -1920, 0  # monitor to the left
        self.drain()
        self.assertTrue(self.visible)
        self.bar.top, self.bar.bottom = 1078, 1126
        w._apply()
        self.assertFalse(self.visible)
        self.bar.top, self.bar.bottom = 1032, 1080
        self.video.left, self.video.right = 0, 1920
        self.minimized.add(3)
        w._apply()
        self.assertTrue(self.visible)
        w.hide()
        self.u.GetForegroundWindow.return_value = 4
        w._apply()
        self.assertFalse(self.visible)

    def test_hook_failure_retries_on_real_update_without_polling(self):
        w, u = self.widget, self.u
        u.SetWinEventHook.return_value = 0
        with self.assertLogs("taskbar_widget", level="WARNING"):
            self.drain()
        u.SetTimer.assert_not_called()
        u.SetWinEventHook.return_value = 100
        self.video.bottom = 800
        w.show()
        self.drain()
        self.assertTrue(self.visible)
        self.assertFalse(w._missing_hooks)
        u.SetTimer.assert_not_called()

    def test_refused_video_thread_hook_recovers_via_filtered_global_events(self):
        m, w, u = self.m, self.widget, self.u
        u.SetWinEventHook.side_effect = lambda first, last, dll, cb, pid, tid, flags: (
            0 if tid == 3 else 100)
        self.stack = [1, 4, 3, 2]
        u.GetForegroundWindow.return_value = 4
        self.drain()
        self.assertFalse(self.visible)
        fallback = (m.EVENT_OBJECT_STATECHANGE, m.EVENT_OBJECT_LOCATIONCHANGE, 0)
        self.assertIn(fallback, w._hooks)
        u.PostMessageW.reset_mock()
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 50)
        u.PostMessageW.assert_not_called()
        self.video.bottom = 600
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 3)
        self.assertTrue(self.visible)
        self.stack.remove(3)
        self.event(m.EVENT_OBJECT_DESTROY, 3)
        self.assertNotIn(fallback, w._hooks)
        u.SetTimer.assert_not_called()

    def test_foreground_switch_to_desktop(self):
        m, w, u = self.m, self.widget, self.u
        self.drain()  # started during fullscreen
        self.assertFalse(self.visible)
        u.GetForegroundWindow.return_value = 4
        self.stack = [1, 4, 2, 3]
        self.rects[4] = self.monitor
        u.GetClassNameW.side_effect = lambda hwnd, buf, n: setattr(buf, "value", "Progman")
        u.PostMessageW.reset_mock()
        w._on_win_event(1, m.EVENT_SYSTEM_FOREGROUND, 4, 0, 0, 4, 0)
        u.PostMessageW.assert_called_once_with(1, m.WM_APP_UPDATE, 0, 0)
        self.drain()
        self.assertTrue(self.visible)

    def test_small_popup_keeps_focus_as_background_cover_changes(self):
        m, w, u = self.m, self.widget, self.u
        self.drain()
        self.stack = [1, 4, 3, 2]
        u.GetForegroundWindow.return_value = 4
        self.event(m.EVENT_SYSTEM_FOREGROUND, 4)
        self.assertFalse(self.visible)
        self.rects[4] = m.wintypes.RECT(1930, 10, 2230, 210)
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 4)  # popup on the second monitor
        self.assertFalse(self.visible)
        w.show()
        w.update(m.Image.new("RGBA", (100, 20)))
        self.drain()
        w._upload.assert_not_called()
        u.SetWindowPos.assert_not_called()

        # Geometry, hide/show, minimize/restore and virtual desktop changes
        # must work even though the popup stays foreground throughout.
        self.video.right, self.video.bottom = 900, 600
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 3)
        self.assertTrue(self.visible)
        self.video.right, self.video.bottom = 1920, 1080
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 3)
        self.assertFalse(self.visible)
        for states, lost, back in (
                (self.hidden, m.EVENT_OBJECT_HIDE, m.EVENT_OBJECT_SHOW),
                (self.minimized, m.EVENT_OBJECT_STATECHANGE, m.EVENT_SYSTEM_MINIMIZEEND),
                (self.cloaked, m.EVENT_OBJECT_CLOAKED, m.EVENT_OBJECT_UNCLOAKED)):
            with self.subTest(event=lost):
                if states is self.minimized:
                    self.event(m.EVENT_SYSTEM_MINIMIZESTART, 3)  # can precede IsIconic
                    self.assertFalse(self.visible)
                states.add(3)
                self.event(lost, 3)
                self.assertTrue(self.visible)
                states.remove(3)
                self.event(back, 3)
                self.assertFalse(self.visible)
        self.stack.remove(3)
        del self.rects[3]  # destroyed HWND can no longer be queried
        self.event(m.EVENT_OBJECT_DESTROY, 3)
        self.assertTrue(self.visible)
        self.assertEqual(w._upload.call_count, 1)
        u.SetTimer.assert_not_called()

    def test_restack_handles_multiple_blockers_and_skips_own_window(self):
        m, u = self.m, self.u
        self.rects[1] = self.monitor  # would cover if included in the walk
        self.rects[5] = self.monitor
        self.stack = [1, 4, 2, 3, 5]
        u.GetForegroundWindow.return_value = 4
        self.drain()
        self.assertTrue(self.visible)
        self.dwm.DwmGetWindowAttribute.assert_not_called()

        self.stack = [1, 4, 5, 3, 2]
        self.event(m.EVENT_OBJECT_REORDER, 99, m.OBJID_CLIENT)
        self.assertFalse(self.visible)
        self.hidden.add(3)
        u.SetWindowPos.reset_mock()
        self.event(m.EVENT_OBJECT_HIDE, 3)
        self.assertFalse(self.visible)
        u.SetWindowPos.assert_not_called()
        self.stack = [1, 4, 2, 5, 3]
        self.event(m.EVENT_OBJECT_REORDER, 99, m.OBJID_CLIENT)
        self.assertTrue(self.visible)

    def test_new_background_window_and_invisible_resize_border(self):
        m = self.m
        self.stack = [1, 4, 2]
        self.u.GetForegroundWindow.return_value = 4
        self.drain()
        self.assertTrue(self.visible)
        self.stack.insert(2, 3)  # show without activating
        self.frames[3] = m.wintypes.RECT(0, 0, 1920, 1079)
        self.event(m.EVENT_OBJECT_SHOW, 3)
        self.assertTrue(self.visible)
        self.frames[3] = self.monitor
        self.event(m.EVENT_OBJECT_LOCATIONCHANGE, 3)
        self.assertFalse(self.visible)

    def test_destroyed_link_is_not_mistaken_for_the_top_of_the_stack(self):
        m, u = self.m, self.u
        self.stack = [1, 3, 4, 2]  # small window 4 immediately above the bar
        get_window = u.GetWindow.side_effect

        def destroy_during_walk(hwnd, relation):
            if hwnd == 4 and relation == m.GW_HWNDPREV and hwnd in self.stack:
                self.stack.remove(hwnd)
            return get_window(hwnd, relation)

        u.GetWindow.side_effect = destroy_during_walk
        self.drain()
        self.assertFalse(self.visible)
        self.widget._upload.assert_not_called()
        u.SetWindowPos.assert_not_called()
        self.assertIn(3, self.widget._watched_windows)

    def test_explorer_restarts_before_new_taskbar_exists(self):
        m, w, u = self.m, self.widget, self.u
        u.LoadCursorW.return_value = 0
        u.GetDpiForWindow.return_value = 96
        u.CreateWindowExW.side_effect = [10, 11, 12]
        u.DestroyWindow.side_effect = lambda hwnd: w._proc(hwnd, m.WM_DESTROY, 0, 0)

        def pump(*args):
            if w.hwnd == 10:  # old owner took its popup down with it
                u.FindWindowW.return_value = 0
                w._proc(10, m.WM_DESTROY, 0, 0)
            elif w.hwnd == 11:  # the unowned, hidden popup gets TaskbarCreated
                u.FindWindowW.return_value = 5
                w._proc(11, w._msg_taskbar_created, 0, 0)
                w._proc(11, m.WM_APP_UPDATE, 0, 0)
            else:
                self.owner = 5
                self.stack = [12, 3, 5]
                self.rects[5] = self.bar
                self.video.bottom = 800
                w._apply()
                w.close()
                w._proc(12, m.WM_CLOSE, 0, 0)
            return 0

        u.GetMessageW.side_effect = pump
        w._run()
        owners = [call.args[8] for call in u.CreateWindowExW.call_args_list]
        self.assertEqual(owners, [2, 0, 5])
        w._upload.assert_called_once()
        self.assertEqual(w.hwnd, 0)
