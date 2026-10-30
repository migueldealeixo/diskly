import errno
import os
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from diskly.domain.scanning import IssueCode, ScanStatus
from diskly.infrastructure.linux.mounts import MountInfo, read_mount_id, read_mount_table
from diskly.infrastructure.persistence.identity_index import IdentityIndex
from diskly.scanner import engine, policy, scan_directory


def test_user_exclusions_cover_files_and_subtrees_without_prefix_matches(tmp_path: Path) -> None:
    for name in ("cache", "cache-backup"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "data.bin").write_bytes(b"abc")
    (tmp_path / "excluded.bin").write_bytes(b"ignored")

    result = scan_directory(tmp_path, exclude_paths=["cache", tmp_path / "excluded.bin"])

    assert result.status is ScanStatus.COMPLETED
    assert result.logical_bytes == 3
    assert result.directories_count == 2
    assert result.excluded_entries_count == 2
    assert tmp_path / "cache" in result.excluded_paths
    assert result.issues_count == 0


@pytest.mark.parametrize("excluded", [".", ".."])
def test_excluded_root_is_rejected(tmp_path: Path, excluded: str) -> None:
    with pytest.raises(ValueError, match="root is excluded"):
        scan_directory(tmp_path, exclude_paths=[excluded])


@pytest.mark.parametrize(
    "variable", ["XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"]
)
def test_diskly_application_directories_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    base = tmp_path / "xdg"
    application = base / "diskly"
    application.mkdir(parents=True)
    (application / "internal.bin").write_bytes(b"internal")
    (base / "personal.bin").write_bytes(b"ok")
    monkeypatch.setenv(variable, str(base))

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.COMPLETED
    assert result.logical_bytes == 2
    assert result.excluded_entries_count == 1
    assert application in result.excluded_paths


def test_relative_xdg_paths_use_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/cache")

    assert tmp_path / ".cache/diskly" in policy.application_paths()


def test_scratch_index_is_excluded_and_removed(tmp_path: Path) -> None:
    (tmp_path / "data.bin").write_bytes(b"abc")

    result = scan_directory(tmp_path, scratch_directory=tmp_path)

    assert result.status is ScanStatus.COMPLETED
    assert result.logical_bytes == 3
    assert result.files_count == 1
    assert result.excluded_entries_count == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data.bin"]


def test_application_exclusions_resolve_xdg_ancestor_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual-cache"
    application = actual / "diskly"
    application.mkdir(parents=True)
    (application / "internal.bin").write_bytes(b"internal")
    alias = tmp_path / "alias-cache"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(alias))

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.COMPLETED
    assert result.logical_bytes == 0
    assert result.excluded_entries_count == 1
    assert application in result.excluded_paths


def test_exclusions_work_when_root_ancestors_are_symlinks(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    root = actual / "root"
    excluded = root / "excluded"
    excluded.mkdir(parents=True)
    (excluded / "data.bin").write_bytes(b"ignored")
    (root / "included.bin").write_bytes(b"ok")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)

    result = scan_directory(alias / "root", exclude_paths=[alias / "root/excluded"])

    assert result.logical_bytes == 2
    assert result.excluded_entries_count == 1
    assert excluded in result.excluded_paths


def test_mount_snapshot_skips_same_device_mounts_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "mounted"
    child.mkdir()
    (child / "outside.bin").write_bytes(b"outside")
    (tmp_path / "inside.bin").write_bytes(b"ok")
    table = read_mount_table()
    mount_id = max(table) + 1
    table[mount_id] = MountInfo(mount_id, child, "ext4")
    monkeypatch.setattr(policy, "read_mount_table", lambda: table)
    open_directory = engine._open_directory

    def reject_mounted_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        assert path != child
        return open_directory(path, parent_fd=parent_fd)

    monkeypatch.setattr(engine, "_open_directory", reject_mounted_open)

    result = scan_directory(tmp_path)

    assert child.stat().st_dev == tmp_path.stat().st_dev
    assert result.status is ScanStatus.PARTIAL
    assert result.logical_bytes == 2
    assert result.skipped_mounts_count == 1
    assert result.issues[0].code is IssueCode.MOUNT_BOUNDARY


def test_file_bind_mounts_are_excluded_from_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mounted = tmp_path / "mounted.bin"
    mounted.write_bytes(b"outside")
    table = read_mount_table()
    mount_id = max(table) + 1
    table[mount_id] = MountInfo(mount_id, mounted, "ext4")
    monkeypatch.setattr(policy, "read_mount_table", lambda: table)

    result = scan_directory(tmp_path)

    assert result.logical_bytes == 0
    assert result.files_count == 0
    assert result.skipped_mounts_count == 1
    assert result.status is ScanStatus.PARTIAL


def test_different_device_files_missing_from_snapshot_are_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "mounted.bin"
    file.write_bytes(b"outside")
    fields = list(file.stat())
    fields[2] += 1
    entry = Mock(spec=os.DirEntry)
    entry.name = file.name
    entry.stat.return_value = os.stat_result(fields)
    open_directory = engine._open_directory

    def replace_entries(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        frame.entries = iter([entry])
        return frame

    monkeypatch.setattr(engine, "_open_directory", replace_entries)

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.PARTIAL
    assert result.files_count == 0
    assert result.logical_bytes == 0
    assert result.issues[0].code is IssueCode.DEVICE_BOUNDARY


def test_descriptor_check_detects_mounts_missing_from_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "mounted"
    child.mkdir()
    (child / "outside.bin").write_bytes(b"outside")
    monkeypatch.setattr(engine, "read_mount_id", lambda descriptor: read_mount_id(descriptor) + 1)

    result = scan_directory(tmp_path)

    assert child.stat().st_dev == tmp_path.stat().st_dev
    assert result.status is ScanStatus.PARTIAL
    assert result.logical_bytes == 0
    assert result.skipped_directories_count == 1
    assert result.skipped_mounts_count == 1
    assert result.issues[0].code is IssueCode.MOUNT_BOUNDARY


def test_unverifiable_child_mount_is_skipped_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "child").mkdir()
    descriptors: list[int] = []

    def fail_mount_check(descriptor: int) -> int:
        descriptors.append(descriptor)
        raise OSError(errno.EIO, "Mount information unavailable")

    monkeypatch.setattr(engine, "read_mount_id", fail_mount_check)

    result = scan_directory(tmp_path)

    assert result.status is ScanStatus.PARTIAL
    assert result.issues[0].code is IssueCode.POLICY_UNAVAILABLE
    assert len(descriptors) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(descriptors[0])
    assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize(
    "filesystem_type", ["proc", "sysfs", "nfs4", "cifs", "fuse.sshfs", "unknown"]
)
def test_unsupported_root_filesystems_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filesystem_type: str
) -> None:
    table = {
        mount_id: MountInfo(mount_id, mount.mount_point, filesystem_type)
        for mount_id, mount in read_mount_table().items()
    }
    monkeypatch.setattr(policy, "read_mount_table", lambda: table)

    with pytest.raises(OSError) as caught:
        scan_directory(tmp_path)

    assert caught.value.errno == errno.ENOTSUP


def test_missing_root_mount_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "read_mount_table", lambda: {})

    with pytest.raises(OSError) as caught:
        scan_directory(tmp_path)

    assert caught.value.errno == errno.ESTALE


def test_index_failure_returns_partial_and_cleans_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "child").mkdir()
    descriptors: list[int] = []
    open_directory = engine._open_directory
    add_directory = IdentityIndex.add_directory
    calls = 0

    def record_open(path: Path, *, parent_fd: int | None = None) -> engine._DirectoryFrame:
        frame = open_directory(path, parent_fd=parent_fd)
        descriptors.append(frame.descriptor)
        return frame

    def fail_after_root(index: IdentityIndex, device: int, inode: int) -> bool:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise sqlite3.OperationalError("database or disk is full")
        return add_directory(index, device, inode)

    monkeypatch.setattr(engine, "_open_directory", record_open)
    monkeypatch.setattr(IdentityIndex, "add_directory", fail_after_root)

    result = scan_directory(tmp_path, scratch_directory=tmp_path)

    assert result.status is ScanStatus.PARTIAL
    assert result.issues[0].code is IssueCode.STORAGE_ERROR
    assert sorted(path.name for path in tmp_path.iterdir()) == ["child"]
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


def test_unusable_index_budget_is_rejected_before_scanning(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_index_bytes"):
        scan_directory(tmp_path, max_index_bytes=0)


def test_real_index_budget_exhaustion_returns_partial(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for number in range(1000):
        (root / str(number)).mkdir()

    result = scan_directory(root, scratch_directory=scratch, max_index_bytes=16 * 1024)

    assert result.status is ScanStatus.PARTIAL
    assert result.directories_count < 1001
    assert result.issues[0].code is IssueCode.STORAGE_ERROR
    assert list(scratch.iterdir()) == []


def test_interrupted_mount_check_closes_child_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "child").mkdir()
    descriptors: list[int] = []

    def interrupt_mount_check(descriptor: int) -> int:
        descriptors.append(descriptor)
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "read_mount_id", interrupt_mount_check)

    with pytest.raises(KeyboardInterrupt):
        scan_directory(tmp_path)

    assert len(descriptors) == 1
    with pytest.raises(OSError) as caught:
        os.fstat(descriptors[0])
    assert caught.value.errno == errno.EBADF
