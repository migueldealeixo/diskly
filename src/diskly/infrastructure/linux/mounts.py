import errno
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
_ESCAPE_PATTERN = re.compile(r"\\(040|011|012|134)")


@dataclass(frozen=True, slots=True)
class MountInfo:
    mount_id: int
    mount_point: Path
    filesystem_type: str


def parse_mountinfo(lines: Iterable[str]) -> dict[int, MountInfo]:
    mounts: dict[int, MountInfo] = {}
    for line in lines:
        before, separator, after = line.partition(" - ")
        fields = before.split()
        filesystem_fields = after.split()
        if not separator or len(fields) < 6 or len(filesystem_fields) < 3:
            raise ValueError("Malformed mountinfo record")
        mount_id = int(fields[0])
        mount_point = Path(_ESCAPE_PATTERN.sub(lambda match: _ESCAPES[match[1]], fields[4]))
        if mount_id <= 0 or mount_id in mounts or not mount_point.is_absolute():
            raise ValueError("Invalid mount identity or path")
        mounts[mount_id] = MountInfo(mount_id, mount_point, filesystem_fields[0])
    if not mounts:
        raise ValueError("Empty mount table")
    return mounts


def read_mount_table() -> dict[int, MountInfo]:
    try:
        with Path("/proc/self/mountinfo").open(
            encoding="utf-8", errors="surrogateescape"
        ) as stream:
            return parse_mountinfo(stream)
    except ValueError as error:
        raise OSError(errno.EIO, "Cannot interpret the process mount table") from error


def read_mount_id(descriptor: int) -> int:
    try:
        with Path(f"/proc/self/fdinfo/{descriptor}").open(encoding="ascii") as stream:
            for line in stream:
                key, separator, value = line.partition(":")
                if key == "mnt_id" and separator:
                    mount_id = int(value)
                    if mount_id > 0:
                        return mount_id
                    break
    except ValueError as error:
        raise OSError(errno.EIO, "Cannot interpret the descriptor mount identity") from error
    raise OSError(errno.EIO, "Descriptor mount identity is unavailable")


def read_descriptor_path(descriptor: int) -> Path:
    path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    if not path.is_absolute() or os.fstat(descriptor).st_nlink == 0:
        raise OSError(errno.ESTALE, "Directory no longer has a stable absolute path")
    return path
