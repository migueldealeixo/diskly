import sqlite3
import struct
from pathlib import Path

MIN_INDEX_BYTES = 16 * 1024
DEFAULT_MAX_INDEX_BYTES = 256 * 1024 * 1024


class IdentityIndex:
    def __init__(self, path: Path, *, max_bytes: int = DEFAULT_MAX_INDEX_BYTES) -> None:
        if max_bytes < MIN_INDEX_BYTES:
            raise ValueError(f"max_bytes must be at least {MIN_INDEX_BYTES}")
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._pending = 0
        try:
            self._connection.execute("PRAGMA page_size = 4096")
            self._connection.execute("PRAGMA cache_size = -4096")
            self._connection.execute("PRAGMA mmap_size = 0")
            self._connection.execute("PRAGMA journal_mode = OFF")
            self._connection.execute("PRAGMA synchronous = OFF")
            self._connection.execute(f"PRAGMA max_page_count = {max_bytes // 4096}")
            self._connection.execute(
                "CREATE TABLE identities (kind INTEGER NOT NULL, identity BLOB NOT NULL, "
                "PRIMARY KEY (kind, identity)) WITHOUT ROWID"
            )
            self._connection.execute("BEGIN")
        except BaseException:
            self._connection.close()
            raise

    def add_directory(self, device: int, inode: int) -> bool:
        return self._add(0, device, inode)

    def add_file(self, device: int, inode: int) -> bool:
        return self._add(1, device, inode)

    def _add(self, kind: int, device: int, inode: int) -> bool:
        identity = struct.pack("!QQ", device, inode)
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO identities (kind, identity) VALUES (?, ?)",
            (kind, identity),
        )
        added = cursor.rowcount == 1
        self._pending += int(added)
        if self._pending >= 1000:
            self._connection.execute("COMMIT")
            self._connection.execute("BEGIN")
            self._pending = 0
        return added

    def close(self) -> None:
        self._connection.close()
