import unicodedata


def safe_text(value: object) -> str:
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char).startswith("C")
        else char
        for char in str(value)
    )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    if value < 1024:
        return f"{value:,} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        amount /= 1024
        if amount < 1024 or unit == "EiB":
            return f"{amount:,.1f} {unit}"
    raise AssertionError("Unreachable byte unit")
