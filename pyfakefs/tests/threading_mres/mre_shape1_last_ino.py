"""
Pass C MRE — Shape 1: Integer RMW race on last_ino (duplicate inode numbers)

Hazard:   pyfakefs/fake_file.py:565-566
Shape:    Shape 1 — integer read-modify-write counter (THREAD-SAFETY-AUDIT.md)
Observed: Silent invariant violation — duplicate inode numbers across files

Pre-fix observation:
    CPython 3.13 GIL-disabled: 10/10 repeats produced duplicate inodes.
    PyPy 7.3.19: fires via Pass B invariant check (link_burst, 10/10).

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock wraps all public methods via _with_lock decorator.
    The last_ino += / st_ino = sequence is now atomic within the lock.
    Expected result: 0/N repeats with duplicate inodes.  Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape1_last_ino.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape1_last_ino.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape1_last_ino.py

Background:
    add_entry (fake_file.py:563-567):

        self._entries[path_object_name] = path_object
        path_object.parent_dir = weakref.ref(self)
        if path_object.st_ino is None:
            self.filesystem.last_ino += 1        # line 566 — RMW of shared counter
            path_object.st_ino = self.filesystem.last_ino

    Under CPython with GIL, a single INPLACE_ADD bytecode is atomic but the
    read-then-assign sequence (last_ino += 1; st_ino = last_ino) is not.  Two
    threads can both execute += and then both read the same final value,
    assigning duplicate inodes to different files.

    Under free-threaded CPython (PEP 703) the += itself is non-atomic,
    widening the window further and causing this to fire on every repeat.

    Sibling hazards (same shape):
      - fake_filesystem.py:579 (last_dev in add_mount_point)
      - fake_filesystem.py:588 (last_ino for mount point root path)
      - fake_file.py:568 (st_nlink += in add_entry)
      - fake_file.py:642 (st_nlink -= in remove_entry)
      - fake_filesystem.py:751 (used_size in change_disk_usage)
"""
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator

from pyfakefs.fake_filesystem import FakeFilesystem
from pyfakefs.fake_file import FakeDirectory, FakeFile

THREADS = 16
N_PER = 64
REPEATS = 10


def create_files_worker(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Create n files, each triggering an add_entry that increments last_ino.

    :param fs: Active FakeFilesystem instance (shared across threads).
    :param n: Number of files to create.
    :param prefix: Per-invocation distinguishing prefix to avoid cross-thread
        name collisions.
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.create_file(f"/shared/f_{prefix}_{tid}_{i}", contents="x")
        except OSError:
            pass  # benign: TOCTOU on existence check (Shape 5)


def iter_inodes(node: FakeFile | FakeDirectory) -> Iterator[int]:
    """Recursively yield inode numbers from a fake filesystem subtree.

    :param node: Root node to walk.
    :yields: st_ino values for each node that has one.
    """
    if node.st_ino is not None:
        yield node.st_ino
    if isinstance(node, FakeDirectory):
        for child in list(node.entries.values()):
            yield from iter_inodes(child)


def main() -> None:
    """Run the MRE and assert uniqueness of inode numbers post-burst."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 1 (last_ino RMW) MRE — {interpreter} — {gil_status}")

    violation_repeats = 0
    total_duplicates = 0

    for _repeat in range(REPEATS):
        fs = FakeFilesystem("/")
        fs.create_dir("/shared")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [
                pool.submit(create_files_worker, fs, N_PER, i)
                for i in range(THREADS)
            ]
            for f in futures:
                f.result()

        # Invariant: every file must have a unique inode number.
        inodes = list(iter_inodes(fs.root))
        dups = [ino for ino, count in Counter(inodes).items() if count > 1]
        if dups:
            violation_repeats += 1
            total_duplicates += len(dups)

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Repeats with duplicate inodes: {violation_repeats}/{REPEATS}")
    print(f"Total duplicate inode values: {total_duplicates}")

    # Post-fix assertion: the RLock makes last_ino RMW atomic; duplicates must
    # not appear.  A non-zero count here means the lock was removed or bypassed.
    if violation_repeats:
        print("\nREGRESSION: Shape 1 last_ino RMW produced duplicate inodes.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no duplicate inodes (post-fix invariant holds).")


if __name__ == "__main__":
    main()
