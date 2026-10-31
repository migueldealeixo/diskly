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
    parser.add_argument("--top", type=int, default=10, metavar="COUNT")
    arguments = parser.parse_args()
    if arguments.top < 0:
        parser.error("--top must be non-negative")
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
            top_limit=arguments.top,
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
    if result.root_directory is not None:
        print(f"Root apparent logical bytes: {result.root_directory.apparent_logical_bytes}")
    print(f"Largest files by logical bytes (up to {result.top_limit} unique inodes):")
    for file in result.largest_files:
        print(f"{file.logical_bytes:>12} bytes | links={file.link_count} | {str(file.path)!r}")
    print("Largest directories by apparent logical bytes (overlap; do not add):")
    for directory in result.largest_directories:
        coverage = "complete" if directory.is_complete else "incomplete"
        print(
            f"{directory.apparent_logical_bytes:>12} bytes | {coverage} | {str(directory.path)!r}"
        )
    for issue in result.issues:
        print(f"{issue.code}: {str(issue.path)!r}: {issue.message!r}")


if __name__ == "__main__":
    main()
