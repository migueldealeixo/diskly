# Initial architecture decisions

## Scope

The current increments establish packaging, quality checks, immutable result models,
mount-aware traversal, explicit exclusions, and disk-backed identity indexes.
The scanner can be tested without Qt. This does not complete Diskly v0.1.

The reference platform is Ubuntu 24.04 on x86-64 with Python 3.12. The core uses
only the Python standard library and performs no cleanup operations.

## Module boundaries

- `domain`: result models, status values, and issue codes; no Qt dependencies.
- `scanner`: incremental filesystem traversal producing domain results.
- `infrastructure/linux`: process mount metadata and descriptor identities.
- `infrastructure/persistence`: disposable SQLite identity index.
- `ui`, `application`, detectors, and persistence will be introduced when needed.

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

These metrics must remain explicit when future versions add directory rankings,
categories, metadata accounting, and snapshots.

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

## Remaining work before the desktop MVP

1. Progress events and cooperative cancellation independent of Qt.
2. Directory summaries and bounded rankings with explicit metrics.
3. Qt worker integration and the first desktop screen.
4. Contextual artifact detection and informational recommendations.
5. Broader filesystem, failure-recovery, and large-tree validation.

Tests use disposable temporary directories. No destructive test targets real user
or system directories.

## References

- [Python filesystem interfaces](https://docs.python.org/3.12/library/os.html)
- [Linux mountinfo](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)
- [Linux descriptor metadata](https://man7.org/linux/man-pages/man5/proc_pid_fdinfo.5.html)
- [XDG base directories](https://specifications.freedesktop.org/basedir/latest/)
- [Python SQLite interface](https://docs.python.org/3.12/library/sqlite3.html)
- [SQLite configuration](https://www.sqlite.org/pragma.html)
- [Writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)
- [pytest temporary directories](https://docs.pytest.org/en/stable/how-to/tmp_path.html)
- [Ruff configuration](https://docs.astral.sh/ruff/configuration/)
- [mypy configuration](https://mypy.readthedocs.io/en/stable/config_file.html)
