# Initial architecture decisions

## Scope

The current increments establish packaging, quality checks, immutable result models,
mount-aware traversal, explicit exclusions, disk-backed identity indexes,
progress snapshots, cooperative cancellation, bounded rankings, and directory
summaries, and the first PySide6 desktop interface.
The scanner can be tested without Qt. This does not complete Diskly v0.1.

The reference platform is Ubuntu 24.04 on x86-64 with Python 3.12. The core uses
only the Python standard library and performs no cleanup operations.

## Module boundaries

- `domain`: result models, status values, and issue codes; no Qt dependencies.
- `scanner`: incremental filesystem traversal producing domain results.
- `infrastructure/linux`: process mount metadata and descriptor identities.
- `infrastructure/persistence`: disposable SQLite identity index.
- `ui`: Qt widgets, bounded table models, themes, and a background scan adapter.
- Application services, detectors, and durable persistence will be introduced when needed.

There is no plugin system, service layer, or generic persistence abstraction yet.

## Measurement contract

- Logical bytes are the sum of `st_size` for observed unique regular files.
- Allocated bytes are the sum of `st_blocks * 512`, or unavailable if the metric
  is missing for an observed unique file.
- Directory and symlink metadata are excluded from these initial byte totals.
- File-name counts include hard-link aliases; unique-file counts and byte totals
  deduplicate by `(st_dev, st_ino)` when multiple links are reported.
- Directory counts include the root and successfully entered descendants.
- Sizes are observations, not guarantees of recoverable storage.

These metrics remain explicit as categories, metadata accounting, and snapshots
are introduced. Directory rankings use apparent-size fields to distinguish them
from global unique totals.

## Traversal policy

The scanner uses iterative depth-first traversal and consumes `os.scandir`
incrementally. Descendant directories are opened relative to their parent's file
descriptor with `O_DIRECTORY` and `O_NOFOLLOW`. Their opened identity is compared
with the previously observed identity before traversal. File contents, FIFOs,
sockets, and devices are never opened for reading.

The selected root is opened once. Its final component must not be a symlink;
ancestor symlinks may resolve as part of selecting that root. These precautions do
not provide an atomic snapshot or a general defense against a malicious process
with the same user privileges.

Different-device descendants and revisited directory identities are skipped.
`/proc/self/mountinfo` supplies the initial mount snapshot, including bind mounts.
Escaped mount paths are decoded once; unknown optional fields are ignored.
Known descendant mount entries are skipped before metadata lookup or opening.
This includes directory and regular-file bind mounts.

The root and every opened descendant directory are checked using the `mnt_id`
field in `/proc/self/fdinfo`. A child on another mount is not traversed, even if
its device number matches the root. Failure to verify a child's mount skips that
subtree; failure to establish the root mount aborts setup. Root filesystem types
must appear in the explicit local/memory-backed allowlist. Unknown types,
network, FUSE, and virtual kernel filesystems are not accepted as roots.

Mount snapshots and descriptor checks do not make the analysis atomic. In
particular, newly created file bind mounts or concurrent path renames can affect
coverage. The current scanner is not ready for unrestricted system-wide scans.

Depth is limited to bound open iterators and descriptors. Issue details are capped
while their total count remains available. Encountering errors or omitted subtrees
produces a partial result. Failure to open the root propagates to the caller.

## Exclusions and coverage

User exclusions are exact lexical paths and their descendants, with relative paths
anchored to the selected root. They do not use glob patterns or resolve arbitrary
symlink targets. Ancestor symlinks in the selected root are mapped to its opened
canonical location for comparison with mount points and exclusions.

Diskly's own directories under `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME`,
and `XDG_CONFIG_HOME` are always excluded, including resolved aliases. Relative XDG
settings are ignored in favor of the specification's defaults. No XDG directories
are created merely to establish these exclusions.

The active temporary workspace is also excluded, even if the caller places it
inside the selected tree. The result reports configured exclusions within the
canonical root and the number of encountered excluded entries, without enumerating
their descendants. A root excluded by policy is rejected.

Explicit exclusions define the intended scope and do not themselves make a result
partial. Mount boundaries, access failures, limits, and unavailable metadata do.
The mount counter includes skipped file mounts; the skipped-directory counter
only covers entries already identified as directories. Prefiltered mount entries
are not inspected just to classify their type.

## Temporary identity storage

One SQLite database per scan stores directory identities and multiply linked file
identities. Keys encode unsigned 64-bit device and inode values as binary data,
with separate kinds for directories and files. No scanned file paths are stored
in this index. Duplicate detection uses a unique primary key and parameterized
inserts. Entries are committed in batches of 1000.

The database has a roughly 4 MiB page-cache target, no memory mapping, and a
configurable file budget enforced by SQLite's maximum page count. The default
budget is 256 MiB with a minimum of 16 KiB. The file budget is rounded down to a
whole page and is not a limit on total process RSS or filesystem overhead.

Journaling and synchronous durability are disabled only for this disposable index.
An interrupted or failed index is discarded, never reused as a source of truth.
Future persistent snapshots and cleanup history must use their own durability
policy. Index errors stop traversal with a partial result and an explicit issue;
there is no fallback to an unbounded in-memory set.

The workspace uses a private temporary directory and is removed when resources
close after normal completion or handled exceptions. A forced termination can
leave it behind. Workspace creation failures propagate rather than trigger writes
elsewhere. The caller can select an existing local scratch directory. A tmpfs
scratch location consumes system memory even though Python's indexes are on file.

Open traversal descriptors remain bounded by depth, issue details remain capped,
and no Python collection grows with the number of visited directory or hard-link
identities. Total memory still depends on depth, mount count, configured exclusions,
and library overhead. Large-tree benchmarks remain necessary.

## Bounded rankings and directory aggregation

Each scan maintains separate bounded min-heaps for files and directories, retaining
up to `top_limit` candidates per ranking (200 by default). Replacement compares
logical size, then the absolute path as a Python string; results are sorted once
at finalization. Values themselves are never compared, including when keys tie.
Zero disables both rankings. No list of every scanned file or directory is kept.

File candidates are created only for unique observed inodes. Multiply linked files
use the existing SQLite identity index; the first observed name represents that
inode and supplies its measured sizes and link count. Traversal order can change
this representative name. This is not duplicate-content detection and the stored
link count does not imply all links are inside the selected root.

Each active traversal frame has a fixed-size accumulator for direct file names,
visited directories, sizes, exclusions, and coverage. A child accumulator merges
into its parent when the child closes, without updating every ancestor for every
file. The child's immutable summary is offered to the directory heap and then
discarded unless retained. The selected root has a separate summary and never
occupies a directory-ranking slot.

Directory size fields are explicitly apparent: they count every observed regular
file name, including hard-link aliases. Global size totals and file rankings remain
deduplicated. Parent and descendant directory rankings overlap and must not be
added together or used as exclusive physical-storage attribution. Reported block
allocations can be unavailable and are not estimates of recoverable storage.

Issue-counter deltas at frame entry and exit determine subtree issue counts in
the depth-first traversal. Exclusions define scope and preserve completeness;
failed access, skipped mounts, limits, and unfinished iterators mark affected
subtrees incomplete. The root also reflects scan-wide storage failures. Unentered
directories have no summary rather than an invented zero size.

When traversal stops, remaining frames merge their observed prefixes while closing
resources, retaining incomplete summaries and file candidates collected so far.
Already completed sibling summaries stay complete. A cancelled or partial scan
therefore exposes only the largest observed candidates, with no claim that they
are the largest entries of the unvisited tree. The root summary is always marked
incomplete for a cancelled or partial result.

Additional live ranking memory is proportional to the configured limit and active
depth, plus retained path lengths. SQLite identity-cache and mount/exclusion memory
have the separate limits described above. Finalization time includes sorting the
retained rankings before terminal progress is emitted.

## Remaining work before the desktop MVP

1. Contextual artifact detection and informational recommendations.
2. Broader filesystem, failure-recovery, and large-tree validation.
3. Desktop polish, volume capacity, configurable scope, and distribution validation.

Tests use disposable temporary directories. No destructive test targets real user
or system directories.

## Progress and cancellation contract

`CancellationToken` wraps a standard-library threading event. Cancellation is
idempotent, one-way, and independent of any Qt event loop. Each restarted scan
needs a fresh token. `scan_directory` remains synchronous; the Qt adapter owns
background execution.

The scanner polls cancellation before and between setup stages, iterator work,
metadata access, and index updates. Child-directory resources have explicit
ownership transfer to the traversal stack, so cancellation before that transfer
also closes the child descriptor. Cooperative checks cannot interrupt a blocked
filesystem call, a callback, or resource cleanup already in progress.

Cancellation returns the observations retained so far with `cancelled` status,
preserving existing issues. It does not erase user data, resume a prior traversal,
or fabricate totals for unvisited entries. A pre-cancelled request performs no
root access and has zero counters and no filesystem type. Numeric validation
still runs first. Setup errors otherwise retain their exception behavior.

Immutable `ScanProgress` snapshots carry the root, current directory, status,
monotonic elapsed time, encountered-entry and file counters, sizes, and issue
count. Entries count names returned by iterators, including excluded names and
names not fully processed after a cancellation or error. Root directories are
counted separately. The accumulator is never shared with consumers.

The default running-update interval is 0.2 seconds. Initial and terminal snapshots
are emitted separately and may be closer together. There is no pre-count or
estimated percentage. With no callback, no progress snapshots are allocated.

Callbacks execute synchronously in the scanning thread, without holding a token
lock. They can request cancellation. UI callbacks enqueue immutable snapshots
for the GUI thread. Exceptions from callbacks are wrapped in
`ProgressCallbackError` with their original cause, then propagated; they must not
be mistaken for failures in the temporary index.

Result finalization and terminal progress happen after descriptors, the SQLite
connection, and the temporary workspace close. The terminal status and counters
match the returned result; cancellation during that terminal callback cannot
retroactively change them. Total elapsed time includes setup, regular callbacks,
and resource cleanup, but excludes terminal callback time. If setup or a callback
raises instead of returning a result, no successful terminal event is guaranteed;
the worker adapter signals that failure explicitly.

## Desktop execution and presentation

PySide6 is an optional runtime dependency. Importing `diskly.scanner` and using the
terminal examples do not import Qt. The launcher imports the UI only after parsing
arguments, so `python -m diskly --help` works without PySide6. A directory argument
only preselects the field and never triggers a scan automatically.

On Linux, application construction passes `-platformtheme generic` to Qt unless
a platform theme was explicitly selected through `QT_QPA_PLATFORMTHEME`. Startup
does not depend on detecting a Snap editor or desktop environment. This bypasses GTK's native
initialization, which can abort on incompatible GNOME schemas before Python can
handle an exception. It does not mutate the process environment or system settings,
and other operating systems retain normal Qt integration. Generic integration
uses Qt dialogs and may limit automatic system appearance detection. Palette
selection and the display backend remain separate from platform-theme selection.

`ScanController` owns one `QThread` per analysis and a fresh cancellation token.
The thread calls the synchronous scanner, emitting immutable snapshots and a
result or error through explicitly queued connections. Controller slots run in
the GUI thread; workers never access widgets. Job IDs reject stale events, and
the controller stays busy until the thread's `finished` signal. Result delivery
alone does not permit another worker to start. Finished threads are released
with `deleteLater`.

Closing while busy records a pending close and requests cancellation. The close
event is deferred until the worker finishes, without calling `wait`, terminating
the thread, or blocking the GUI event loop. Uninterruptible filesystem I/O can
therefore delay closure, as it delays scanner cancellation. No scan can start
while closure is pending.

Tables retain only the result's bounded file/directory rankings and issue sample.
Numeric sorting uses raw values rather than formatted unit strings. Search is a
case-insensitive filter over retained rows, not a persistent filesystem index.
Paths, errors, and details use plain text; control, formatting, and surrogate
characters are escaped before display. Root overview and row details distinguish
unique global totals, overlapping apparent directory sizes, scope, and incomplete
observations. Unknown allocation stays distinct from zero.

The interface uses Fusion with light, dark, or system-selected palettes. Tests
use pytest-qt with Qt's offscreen platform and disposable directories. They cover
real background scans, sorting/filtering, partial results, missing roots,
unavailable allocation, unusual names, stale events, cancellation, deferred close,
GUI-thread delivery, keyboard focus, and palette changes. Event-controlled fake
scans exercise blocked work while verifying that the GUI event loop still runs.

## References

- [Python filesystem interfaces](https://docs.python.org/3.12/library/os.html)
- [Bounded heap operations](https://docs.python.org/3.12/library/heapq.html)
- [Linux mountinfo](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)
- [Linux descriptor metadata](https://man7.org/linux/man-pages/man5/proc_pid_fdinfo.5.html)
- [XDG base directories](https://specifications.freedesktop.org/basedir/latest/)
- [Python SQLite interface](https://docs.python.org/3.12/library/sqlite3.html)
- [Threading events](https://docs.python.org/3.12/library/threading.html#event-objects)
- [Monotonic clock](https://docs.python.org/3.12/library/time.html#time.monotonic)
- [SQLite configuration](https://www.sqlite.org/pragma.html)
- [Writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)
- [pytest temporary directories](https://docs.pytest.org/en/stable/how-to/tmp_path.html)
- [Ruff configuration](https://docs.astral.sh/ruff/configuration/)
- [mypy configuration](https://mypy.readthedocs.io/en/stable/config_file.html)
