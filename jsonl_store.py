"""Transactional, versioned storage for the derived per-transcript index."""
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import zlib

log = logging.getLogger(__name__)


class IndexStore:
    def __init__(self, path: Path, root: Path, pricing):
        self.path = path
        # Bump when parsing, deduplication, or the serialized row format changes.
        self.identity = json.dumps([1, str(root), pricing], sort_keys=True)
        self.replace = True

    def load(self, decode):
        files = {}
        try:
            if not self.path.is_file():
                return files
            uri = self.path.as_uri() + "?mode=rw"
            with closing(sqlite3.connect(uri, uri=True)) as db:
                if db.execute("SELECT identity FROM meta").fetchone() != (self.identity,):
                    return files
                for name, blob in db.execute("SELECT path, data FROM files"):
                    files[Path(name)] = decode(json.loads(zlib.decompress(blob)))
            self.replace = False
        except (sqlite3.Error, OSError, ValueError, TypeError, IndexError, zlib.error):
            log.warning("Rebuilding unreadable JSONL index")
            files.clear()
        return files

    def save(self, files, dirty):
        """Only changed rows on a checkpoint. Replace an unusable DB as a unit."""
        tmp = None
        try:
            if self.replace:
                fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".",
                                           suffix=".tmp")
                os.close(fd)
            with closing(sqlite3.connect(tmp or self.path, timeout=0.25)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                if self.replace:
                    db.execute("CREATE TABLE meta(identity TEXT NOT NULL)")
                    db.execute("INSERT INTO meta VALUES (?)", (self.identity,))
                    db.execute("CREATE TABLE files(path TEXT PRIMARY KEY, data BLOB NOT NULL)")
                    changes = files.items()
                else:
                    if db.execute("SELECT identity FROM meta").fetchone() != (self.identity,):
                        return False  # another app version owns this index now
                    changes = dirty.items()
                for path, entry in changes:
                    if entry is not None:
                        raw = json.dumps(entry.pack(), separators=(",", ":")).encode()
                        db.execute("INSERT OR REPLACE INTO files VALUES (?, ?)",
                                   (str(path), zlib.compress(raw, 1)))
                    else:
                        db.execute("DELETE FROM files WHERE path=?", (str(path),))
            if tmp:
                os.replace(tmp, self.path)
            self.replace = False
            return True
        except (OSError, sqlite3.Error) as exc:
            log.warning("Could not checkpoint JSONL index: %s", exc)
            if isinstance(exc, sqlite3.DatabaseError) and getattr(exc, "sqlite_errorcode", None) in (
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_ERROR):
                self.replace = True
            return False
        finally:
            if tmp:
                try:
                    Path(tmp).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("Could not remove temporary JSONL index: %s", exc)
