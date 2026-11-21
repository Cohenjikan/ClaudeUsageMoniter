"""One outstanding Windows directory read, polled by the existing JSONL thread."""
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import struct


class _Overlapped(c.Structure):
    _fields_ = [("internal", c.c_size_t), ("high", c.c_size_t),
                ("offset", w.DWORD), ("offset_high", w.DWORD), ("event", w.HANDLE)]


_kernel = c.WinDLL("kernel32", use_last_error=True)


def _api(name, result, *args):
    func = getattr(_kernel, name)
    func.restype = result
    func.argtypes = args
    return func


_open = _api("CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
             w.DWORD, w.DWORD, w.HANDLE)
_event = _api("CreateEventW", w.HANDLE, w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR)
_close = _api("CloseHandle", w.BOOL, w.HANDLE)
_read = _api("ReadDirectoryChangesW", w.BOOL, w.HANDLE, w.LPVOID, w.DWORD,
             w.BOOL, w.DWORD, w.LPVOID, c.POINTER(_Overlapped), w.LPVOID)
_result = _api("GetOverlappedResult", w.BOOL, w.HANDLE, c.POINTER(_Overlapped),
               c.POINTER(w.DWORD), w.BOOL)
_cancel = _api("CancelIoEx", w.BOOL, w.HANDLE, c.POINTER(_Overlapped))


class DirectoryWatch:
    def __init__(self, root):
        # FILE_LIST_DIRECTORY, share read/write/delete, OPEN_EXISTING,
        # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED.
        self.handle = _open(str(root), 1, 7, None, 3, 0x42000000, None)
        if self.handle == w.HANDLE(-1).value:
            raise c.WinError(c.get_last_error())
        self.buffer = c.create_string_buffer(65536)
        self.event = _event(None, True, False, None)
        self.pending = False
        try:
            if not self.event:
                raise c.WinError(c.get_last_error())
            self._arm()
        except OSError:
            self.close()
            raise

    def _arm(self):
        self.io = _Overlapped(event=self.event)
        # File/dir names, size and last write. NTFS buffers changes between reads.
        if not _read(self.handle, self.buffer, len(self.buffer), True, 0x1B,
                     None, c.byref(self.io), None):
            raise c.WinError(c.get_last_error())
        self.pending = True

    def changes(self) -> set[Path] | None:
        """Relative file paths, or None when enumeration is needed."""
        paths = set()
        count = w.DWORD()
        for _ in range(8):  # also bound work under sustained notification traffic
            ok = _result(self.handle, c.byref(self.io), c.byref(count), False)
            if not ok and c.get_last_error() == 996:  # ERROR_IO_INCOMPLETE
                return paths
            self.pending = False
            if not ok and c.get_last_error() != 1022:  # ERROR_NOTIFY_ENUM_DIR
                raise c.WinError(c.get_last_error())
            packet = self.buffer[:count.value] if ok else b""
            self._arm()
            if not packet:
                return None
            pos = 0
            while True:
                step, action, size = struct.unpack_from("<III", packet, pos)
                name = packet[pos + 12:pos + 12 + size].decode("utf-16-le", "surrogatepass")
                if action in (4, 5):
                    # Re-enumerate case-only renames too. The original spelling
                    # of the basename participates in fallback session/dedup IDs.
                    return None
                if name.lower().endswith(".jsonl"):
                    paths.add(Path(name))
                elif action != 3:
                    # A directory (with arbitrary descendants) may have moved.
                    return None
                if not step:
                    break
                pos += step
        return None

    def close(self):
        if self.pending:
            _cancel(self.handle, c.byref(self.io))
            # Keep the buffer and OVERLAPPED alive through cancellation completion.
            _result(self.handle, c.byref(self.io), c.byref(w.DWORD()), True)
            self.pending = False
        if self.event:
            _close(self.event)
        _close(self.handle)
