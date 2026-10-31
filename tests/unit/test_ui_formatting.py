import pytest

from diskly.ui.formatting import format_bytes, safe_text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "Unavailable"),
        (0, "0 B"),
        (1023, "1,023 B"),
        (1024, "1.0 KiB"),
        (1024**3, "1.0 GiB"),
        (1536, "1.5 KiB"),
    ],
)
def test_byte_units_keep_unknown_distinct_from_zero(value: int | None, expected: str) -> None:
    assert format_bytes(value) == expected


def test_filename_display_escapes_controls_and_surrogates_without_losing_unicode() -> None:
    assert safe_text("café\n\t\x1b\x7f\udcff\u202e") == "café\\n\\t\\x1b\\x7f\\udcff\\u202e"
    assert safe_text("<b>plain</b>") == "<b>plain</b>"
