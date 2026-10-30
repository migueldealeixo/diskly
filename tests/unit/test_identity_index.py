import sqlite3
from pathlib import Path

import pytest

from diskly.infrastructure.persistence.identity_index import IdentityIndex


def test_identity_index_separates_kinds_devices_and_unsigned_inodes(tmp_path: Path) -> None:
    index = IdentityIndex(tmp_path / "index.sqlite3")
    try:
        assert index.add_directory(1, 2**63 + 1)
        assert not index.add_directory(1, 2**63 + 1)
        assert index.add_file(1, 2**63 + 1)
        assert not index.add_file(1, 2**63 + 1)
        assert index.add_file(2, 2**63 + 1)
        assert index.add_file(2**64 - 1, 2**64 - 1)
    finally:
        index.close()


def test_identity_index_retains_duplicates_across_batches(tmp_path: Path) -> None:
    index = IdentityIndex(tmp_path / "index.sqlite3")
    try:
        for inode in range(2001):
            assert index.add_directory(1, inode)
        for inode in (0, 999, 1000, 1999, 2000):
            assert not index.add_directory(1, inode)
    finally:
        index.close()


def test_identity_index_stops_at_disk_budget(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"
    index = IdentityIndex(path, max_bytes=16 * 1024)
    try:
        with pytest.raises(sqlite3.OperationalError, match="full"):
            for inode in range(10000):
                index.add_directory(1, inode)
    finally:
        index.close()

    assert path.stat().st_size <= 16 * 1024


def test_identity_index_rejects_unusable_budget(tmp_path: Path) -> None:
    path = tmp_path / "index.sqlite3"

    with pytest.raises(ValueError):
        IdentityIndex(path, max_bytes=0)

    assert not path.exists()
