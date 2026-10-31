# Diskly

Diskly is an offline storage analysis project for Linux. Its goal is to explain
where disk space goes, identify development artifacts, and describe the impact of
potential cleanup operations.

**Status:** early development. The current implementation includes a read-only
Python scanner and an initial PySide6 desktop interface with background analysis,
progress reporting, cooperative cancellation, and bounded rankings. Artifact
detectors and cleanup operations are not implemented.

## Development setup

The reference environment is Ubuntu 24.04 with Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

The scanner itself has no third-party runtime dependencies. The `gui` extra adds
PySide6; `dev` also includes PySide6 and pytest-qt for interface development.
Installing dependencies requires network access; analysis works offline.

## Desktop interface

With the environment activated:

```bash
python -m diskly
python -m diskly /path/to/a/directory
```

An editable install also provides the `diskly` launcher. To install only the
application and its desktop dependencies, use `python -m pip install -e '.[gui]'`.
The optional directory argument only fills the directory field; analysis starts
when you select **Analyze**. A normal desktop session is required to display the
window.

On Linux, Diskly selects Qt's generic platform theme before creating the
application. This avoids native GTK initialization crashes involving the missing
`xsettings` `antialiasing` key, including in terminals launched from the VS Code
Snap. Selection does not depend on editor or desktop environment variables and
leaves system settings untouched. An explicit `QT_QPA_PLATFORMTHEME` selection
takes precedence. Generic integration
uses Qt dialogs; automatic system appearance detection may be limited, while
**Light** and **Dark** remain available.

If an inherited platform-theme override triggers the same GTK startup error, try:

```bash
QT_QPA_PLATFORMTHEME=generic python -m diskly
```

- Choose a local directory with **Browse**, or enter its path. **Ctrl+L** focuses
  the directory field; **Ctrl+O** opens the directory picker.
- **Analyze** starts a single background worker. The interface shows observed
  logical size, reported allocation, unique files, visited directories, elapsed
  time, entries, issues, and current path. Activity is indeterminate because the
  final entry count is unknown.
- **Cancel** or **Escape** requests cooperative cancellation. Closing during a
  scan requests cancellation and keeps the window responsive until the worker
  finishes. A blocked filesystem operation can delay cancellation and closure.
- Completed, partial, cancelled, and failed analyses have distinct messages.
  Empty directories have zero byte totals; unavailable allocation is labelled
  **Unavailable**, never zero.
- File, directory, and issue tables support sorting and case-insensitive search.
  Search covers retained rows only: at most 200 files, 200 directories, and the
  first 100 issues with the current defaults. A cancelled or partial ranking
  contains only observed candidates. Selecting a row displays its details.
- Global sizes deduplicate hard links. Directory sizes are apparent and overlap
  between parents and children; they must not be added. Allocation is not a
  promise of recoverable space. Root coverage and exclusions appear in the
  initial result details.
- **System**, **Light**, and **Dark** themes are available. Paths and errors are
  displayed as plain text, with control characters and undecodable bytes escaped.

The desktop currently uses the scanner's default traversal policy, exclusions,
limits, and temporary storage. Custom exclusions and limits remain available via
the Python API and terminal example. There is no persistent inventory or global
filesystem search, and volume capacity is not yet displayed.

## Scanner API

```python
from diskly.scanner import scan_directory

result = scan_directory(
    "/path/to/a/directory",
    exclude_paths=["generated", "large-download.bin"],
)
print(result.status)
print(result.logical_bytes)
print(result.allocated_bytes)
print(result.issues_count)
```

- Directory counts include the selected root and successfully entered descendants.
- File counts include regular-file names; unique counts and byte totals deduplicate
  observed hard links by device and inode.
- Byte totals cover regular files only, excluding directory and symlink metadata.
- Allocated bytes use the filesystem's reported block allocation, not an estimate
  of exclusive physical storage or space that can be recovered.
- Child symlinks are counted but never followed. A symlink as the final root
  component is rejected; ancestors of the user-selected root may resolve through
  symlinks.
- Descendant mounts are skipped, including same-device bind mounts and file bind
  mounts visible in the initial mount table. Directory descriptors are also
  checked against the root mount identity before traversal.
- Root filesystems must be on the local/memory-backed allowlist in
  `src/diskly/scanner/policy.py`. Network, FUSE, virtual kernel, and unknown root
  filesystems are rejected. Mount information must be available through `/proc`.
- Exclusions match paths and their descendants, without glob expansion or textual
  prefix matching. Relative exclusions are anchored to the selected root. Explicit
  user exclusions do not follow symlinks; root-ancestor aliases are normalized.
- Diskly's own XDG cache, state, data, and configuration directories are excluded,
  as is the temporary workspace for the current scan.
- `excluded_entries_count` counts encountered excluded entries, not their hidden
  descendants. `excluded_paths` reports exclusions within the canonical root.
  Intentional exclusions preserve completed status relative to this scope;
  mount boundaries and other incomplete observations produce partial results.
- `skipped_mounts_count` counts mount entries. `skipped_directories_count` counts
  skipped entries already identified as directories; prefiltered mounts are not
  inspected merely to determine their type.
- Root access failures raise an `OSError`; descendant failures and traversal limits
  return a partial result. Special files are counted without being opened.
- Unsupported roots or missing mount information raise an `OSError`. An explicitly
  excluded root raises a `ValueError`.

Directory and hard-link identities are stored in a disposable SQLite database.
The page-cache target is approximately 4 MiB, memory mapping is disabled, and the
database defaults to a 256 MiB size limit. SQLite failures stop the scan with a
partial result and a `storage_error` issue; no in-memory fallback is used.

Use `scratch_directory` to choose an existing local directory for temporary work
and `max_index_bytes` to change the database budget (minimum 16 KiB). The workspace
is private and removed after normal completion or handled failures. Failure to
create it propagates to the caller. Only application-owned temporary data is
written; scanned user files are not modified.

## Largest observed entries and directory summaries

```python
result = scan_directory("/path/to/a/directory", top_limit=20)

for file in result.largest_files:
    print(file.logical_bytes, file.allocated_bytes, repr(str(file.path)))

for directory in result.largest_directories:
    print(
        directory.apparent_logical_bytes,
        directory.is_complete,
        repr(str(directory.path)),
    )
```

- Each ranking retains at most `top_limit` entries (200 by default). Zero disables
  rankings while retaining totals and the root-directory summary.
- Rankings use logical size descending, with the Python string representation of
  the absolute path ascending to break ties. Allocated sizes are also reported
  when available, but selection is not based on allocated space.
- Files are ranked once per observed unique inode. For hard links, the first
  encountered name represents the inode; its path can vary with traversal order.
  The reported link count can include links outside the selected root.
- Directory summaries cover regular-file names in their visited subtrees. Their
  apparent sizes count each hard-link name, even when global totals deduplicate
  the underlying inode. Directory counts include the summarized directory itself.
- `root_directory` reports the selected root separately; the root is not included
  in `largest_directories`. It is unavailable if traversal never started.
- Parent and child directory summaries overlap. Do not add them or treat the
  directory ranking as a partition of storage or an estimate of recoverable space.
- `is_complete` means the subtree was exhausted without traversal issues within
  its configured scope. Explicit exclusions preserve completeness and are counted.
  Omitted mounts, errors, limits, and interrupted traversal mark affected summaries
  incomplete; unentered directories are not reported as zero-sized directories.
- Cancelled or partial scans rank only observed data. Incomplete directory sizes
  are observed prefixes, not complete subtree sizes or globally largest results.
  Fully visited sibling subtrees can retain complete summaries.
- Per-directory issue counts cover traversal issues within the subtree; a storage
  failure that stops the scan is also reflected in the root summary and scan issues.
- All summaries and result tuples are immutable. Rankings store only bounded
  candidates, and directory aggregation uses the depth-bounded traversal stack.

## Progress and cancellation

```python
from diskly.scanner import CancellationToken, ScanProgress, scan_directory

token = CancellationToken()


def display(progress: ScanProgress) -> None:
    print(progress.status, progress.entries_count, progress.logical_bytes)


result = scan_directory(
    "/path/to/a/directory",
    cancellation_token=token,
    on_progress=display,
)
```

Call `token.cancel()` from another thread, a progress callback, or a UI
controller. The token is thread-safe and one-way: create a new token to restart.
The scanner itself remains synchronous and does not create a background worker.

- Progress values are immutable snapshots with the current directory, entry/file
  counts, logical and allocated bytes, issue count, status, and elapsed time.
- Entry counts include encountered child names, including exclusions and entries
  that could not be fully processed. The selected root is not an entry.
- Running updates are throttled to one every 0.2 seconds by default, with separate
  initial and terminal notifications. Small scans may emit only those two events.
  `progress_interval` accepts any finite positive interval in seconds.
- No percentage is reported because the final number of entries is unknown.
- Callbacks run in the scanning thread. Keep them quick. The Qt adapter forwards
  snapshots to the GUI thread through queued signals before updating widgets.
- Callback exceptions raise `ProgressCallbackError` with the original exception
  as the cause. They are not treated as filesystem or SQLite failures.
- A cancelled scan returns `status == "cancelled"` and the counters and issues
  accumulated before stopping. Descriptors and temporary storage are closed before
  the terminal callback and before return. Cancellation does not imply resumability.
- A token cancelled before starting bypasses root access and returns zero counters;
  `filesystem_type` is then unavailable (`None`). Invalid numeric options still
  raise validation errors. Cancellation during setup may also leave the filesystem
  type unavailable.
- A cancellation request made by a terminal callback does not change the result
  that has already been finalized.
- Root/setup failures still raise exceptions. They do not produce a successful
  terminal progress event; the caller must handle exceptions as well as results.
- `elapsed_seconds` uses a monotonic clock and includes setup, callbacks, and
  resource cleanup through result finalization, excluding the terminal callback.

Runnable terminal demonstrations:

```bash
python examples/scan_progress.py . --exclude .venv --exclude .git
python examples/scan_progress.py . --exclude .venv --exclude .git --top 5
python examples/scan_progress.py /path/to/a/large/directory --cancel-after 0.1
python examples/scan_progress.py . --cancel-after 0
```

The timed example requests cancellation after the specified number of seconds.
A small scan can finish first. A zero delay deterministically cancels before root
access. The example prints the largest observed files and directories, with a
default of ten entries per ranking; `--top` overrides that limit. Neither the
example nor the scanner deletes scanned files.

## Quality checks

With the development environment activated:

```bash
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pytest
python -m build
```

For interface tests without a desktop display:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/ui
```

The interface test module skips when PySide6 or pytest-qt is absent; installing
`.[dev]` enables it. Core scanner tests also run without desktop dependencies.

Optional local Git hooks use the same environment:

```bash
pre-commit install
```

GitHub Actions runs linting, formatting, type checking, tests, and package builds
on Ubuntu 24.04 and Python 3.12.

## Current limitations

- This is a live observation, not an atomic filesystem snapshot. Concurrent
  changes can affect counts and hard-link detection.
- The SQLite page-cache setting is a target, not a cap on total process memory.
  The traversal stack, mount table, configured exclusions, and bounded issue list
  also use memory. Scratch storage on tmpfs uses system memory.
- Depth defaults to 128 below the root. Deeper subtrees are skipped explicitly.
- Only the first 100 issues are retained by default; the total issue count and
  truncation flag remain available.
- The mount table is a snapshot. Descriptor checks catch newly encountered
  directory mounts, but mounting, unmounting, or renaming paths during a scan can
  still affect coverage. The scan is not a sandbox against a malicious same-user
  process. Restrict development scans to explicitly selected local directories.
- A forced process termination can leave its private temporary workspace behind.
- There is no immediate interruption of blocking filesystem I/O.
- File contents are not read. Directory access may still update access timestamps
  according to filesystem settings.

## Disposable benchmark

```bash
python benchmarks/scan_tree.py --directories 10000
```

The benchmark creates and removes its own temporary tree, with one unique file
and one hard-link alias per directory. It verifies unique and apparent size totals
and bounded ranking lengths, then reports elapsed time, Python allocations
during scanning, and Linux process peak RSS. RSS includes interpreter startup and
fixture creation; Python allocation measurements exclude SQLite's native memory.
This is a repeatable smoke benchmark, not proof of performance on all filesystems.

See [the initial architecture decisions](docs/architecture.md) for scope and
measurement rules. The detailed development plan is maintained locally and is
not required to install, test, or build the project.
