import argparse
import json
import os
import resource
import time
import tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory

from diskly.scanner import scan_directory


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark a disposable local directory tree")
    parser.add_argument("--directories", type=int, default=10000)
    arguments = parser.parse_args()
    count: int = arguments.directories
    if count < 1:
        parser.error("--directories must be positive")

    with TemporaryDirectory(prefix="diskly-benchmark-") as workspace:
        root = Path(workspace) / "tree"
        root.mkdir()
        for number in range(count):
            directory = root / str(number)
            directory.mkdir()
            original = directory / "original.bin"
            original.write_bytes(b"x")
            os.link(original, directory / "alias.bin")

        tracemalloc.start()
        started = time.perf_counter()
        result = scan_directory(root, scratch_directory=workspace)
        elapsed = time.perf_counter() - started
        _, python_peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        if result.logical_bytes != count or result.unique_files_count != count:
            raise RuntimeError("Benchmark fixture was not counted correctly")
        print(
            json.dumps(
                {
                    "directories": result.directories_count,
                    "file_names": result.files_count,
                    "unique_files": result.unique_files_count,
                    "status": result.status,
                    "seconds": round(elapsed, 3),
                    "python_peak_bytes_during_scan": python_peak_bytes,
                    "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
