import errno
import io
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from diskly.infrastructure.linux.mounts import (
    parse_mountinfo,
    read_mount_id,
    read_mount_table,
)


def test_mountinfo_parses_optional_fields_and_escaped_paths() -> None:
    records = parse_mountinfo(
        [r"36 35 98:0 / /mnt/My\040disk\134040 rw shared:1 unknown:2 - ext4 /dev/sda rw"]
    )

    assert records[36].mount_id == 36
    assert records[36].mount_point == Path("/mnt/My disk\\040")
    assert records[36].filesystem_type == "ext4"


@pytest.mark.parametrize(
    "escaped,decoded",
    [(r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")],
)
def test_mountinfo_decodes_kernel_path_escapes(escaped: str, decoded: str) -> None:
    records = parse_mountinfo([f"1 0 8:1 / /mnt/a{escaped}b rw - ext4 /dev/sda rw"])

    assert records[1].mount_point == Path(f"/mnt/a{decoded}b")


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["malformed"],
        ["1 0 8:1 / /mnt rw - ext4"],
        ["bad 0 8:1 / /mnt rw - ext4 /dev/sda rw"],
        ["0 0 8:1 / /mnt rw - ext4 /dev/sda rw"],
        ["1 0 8:1 / relative rw - ext4 /dev/sda rw"],
        ["1 0 8:1 / /mnt rw - ext4 /dev/sda rw"] * 2,
    ],
)
def test_mountinfo_rejects_incomplete_or_ambiguous_records(lines: list[str]) -> None:
    with pytest.raises(ValueError):
        parse_mountinfo(lines)


def test_mount_table_reports_parse_failures_as_io_errors() -> None:
    with patch.object(Path, "open", return_value=io.StringIO("malformed")):
        with pytest.raises(OSError) as caught:
            read_mount_table()

    assert caught.value.errno == errno.EIO


def test_descriptor_mount_id_belongs_to_the_process_mount_table(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mount_id = read_mount_id(descriptor)
    finally:
        os.close(descriptor)

    assert mount_id in read_mount_table()


@pytest.mark.parametrize("content", ["pos: 0\n", "mnt_id: invalid\n", "mnt_id: 0\n"])
def test_descriptor_without_valid_mount_identity_fails_closed(content: str) -> None:
    with patch.object(Path, "open", return_value=io.StringIO(content)):
        with pytest.raises(OSError) as caught:
            read_mount_id(123)

    assert caught.value.errno == errno.EIO
