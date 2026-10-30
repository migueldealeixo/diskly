import argparse
from math import isfinite
from threading import Timer

from diskly.scanner import CancellationToken, ScanProgress, scan_directory


def display_progress(progress: ScanProgress) -> None:
    print(
        f"{progress.status}: {progress.entries_count} entries, "
        f"{progress.files_count} file names, {progress.logical_bytes} bytes, "
        f"{progress.elapsed_seconds:.2f}s | {str(progress.current_path)!r}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Demonstrate Diskly progress and cancellation")
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--cancel-after", type=float, metavar="SECONDS")
    arguments = parser.parse_args()
    cancel_after: float | None = arguments.cancel_after
    if cancel_after is not None and (not isfinite(cancel_after) or cancel_after < 0):
        parser.error("--cancel-after must be finite and non-negative")

    token = CancellationToken()
    timer: Timer | None = None
    if cancel_after == 0:
        token.cancel()
    elif cancel_after is not None:
        timer = Timer(cancel_after, token.cancel)
        timer.start()
    try:
        result = scan_directory(
            arguments.directory,
            exclude_paths=arguments.exclude,
            cancellation_token=token,
            on_progress=display_progress,
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"Scan failed: {error}\n")
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()

    print(f"Status: {result.status}")
    print(f"Unique files: {result.unique_files_count}")
    print(f"Logical bytes: {result.logical_bytes}")
    print(f"Issues: {result.issues_count}")
    for issue in result.issues:
        print(f"{issue.code}: {str(issue.path)!r}: {issue.message!r}")


if __name__ == "__main__":
    main()
