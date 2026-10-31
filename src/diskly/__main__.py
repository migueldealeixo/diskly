import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diskly desktop storage analysis")
    parser.add_argument("directory", nargs="?", help="Preselect a directory without scanning it")
    args = parser.parse_args(argv)
    try:
        from diskly.ui.app import launch
    except ModuleNotFoundError as error:
        if error.name != "PySide6":
            raise
        print(
            "Install the desktop dependencies: python -m pip install -e '.[gui]'", file=sys.stderr
        )
        return 1
    return launch(args.directory)


if __name__ == "__main__":
    raise SystemExit(main())
