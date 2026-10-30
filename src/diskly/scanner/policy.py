import errno
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from diskly.infrastructure.linux.mounts import (
    read_descriptor_path,
    read_mount_id,
    read_mount_table,
)

_LOCAL_FILESYSTEMS = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "jfs",
        "nilfs2",
        "zfs",
        "vfat",
        "msdos",
        "exfat",
        "ntfs3",
        "iso9660",
        "udf",
        "squashfs",
        "overlay",
        "tmpfs",
        "ramfs",
    }
)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def application_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    for variable, default in (
        ("XDG_CACHE_HOME", Path.home() / ".cache"),
        ("XDG_STATE_HOME", Path.home() / ".local/state"),
        ("XDG_DATA_HOME", Path.home() / ".local/share"),
        ("XDG_CONFIG_HOME", Path.home() / ".config"),
    ):
        configured = Path(os.environ.get(variable) or default)
        base = configured if configured.is_absolute() else default
        paths.append(_absolute(base / "diskly"))
    return tuple(paths)


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    root: Path
    canonical_root: Path
    mount_id: int
    filesystem_type: str
    mount_points: frozenset[Path]
    excluded_paths: frozenset[Path]

    def canonical_path(self, path: Path) -> Path:
        if path.is_relative_to(self.root):
            return _absolute(self.canonical_root / path.relative_to(self.root))
        return _absolute(path)

    def is_excluded(self, path: Path) -> bool:
        canonical = self.canonical_path(path)
        return canonical in self.excluded_paths or any(
            parent in self.excluded_paths for parent in canonical.parents
        )

    def is_mount_point(self, path: Path) -> bool:
        return self.canonical_path(path) in self.mount_points

    def with_exclusion(self, path: Path) -> "ScanPolicy":
        return ScanPolicy(
            self.root,
            self.canonical_root,
            self.mount_id,
            self.filesystem_type,
            self.mount_points,
            self.excluded_paths | {self.canonical_path(path)},
        )


def build_policy(
    root: Path, descriptor: int, exclude_paths: Sequence[str | os.PathLike[str]]
) -> ScanPolicy:
    mount_id = read_mount_id(descriptor)
    mounts = read_mount_table()
    if mount_id not in mounts:
        raise OSError(errno.ESTALE, "The root mount is absent from the process mount table")
    filesystem_type = mounts[mount_id].filesystem_type
    if filesystem_type not in _LOCAL_FILESYSTEMS:
        raise OSError(errno.ENOTSUP, f"Unsupported root filesystem: {filesystem_type}")
    canonical_root = read_descriptor_path(descriptor)
    policy = ScanPolicy(
        root,
        canonical_root,
        mount_id,
        filesystem_type,
        frozenset(
            mount.mount_point
            for mount in mounts.values()
            if mount.mount_point != canonical_root
            and mount.mount_point.is_relative_to(canonical_root)
        ),
        frozenset(),
    )
    for application_path in application_paths():
        policy = policy.with_exclusion(application_path)
        policy = policy.with_exclusion(application_path.resolve(strict=False))
    for path in exclude_paths:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        policy = policy.with_exclusion(candidate)
    if policy.is_excluded(root):
        raise ValueError("The selected root is excluded by the scan policy")
    return policy
