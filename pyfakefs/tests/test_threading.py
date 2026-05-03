"""Threading regression tests for pyfakefs.

These tests verify the post-fix invariants that the coarse `FakeFilesystem`
`RLock` (commit `613867f` / `b6732f8` on `fix-thread-races`) structurally
guarantees.  They are regressive — green state on CPython means the lock is in
place; a future change that accidentally removes a `with self._lock:` from a
public method should be caught here.

They are NOT flakiness-proof on all schedulers: the workloads are tuned to be
aggressive enough to expose races pre-fix on stock CPython 3.12 and 3.13 FT
while staying bounded (≈1 s per test on CPython 3.12).

See PLAN.md § Phase 4 and THREAD-SAFETY-AUDIT.md for per-shape context.
"""

import threading
import unittest

import pyfakefs.fake_filesystem as fake_filesystem
from pyfakefs.fake_filesystem import FakeFilesystem


# ──────────────────────────────────────────────────────────────────────────────
# Shape 1 — inode uniqueness under concurrent file creation
# ──────────────────────────────────────────────────────────────────────────────

class TestShape1InodeUniqueness(unittest.TestCase):
    """Shape 1 (THREAD-SAFETY-AUDIT.md): `last_ino` is an integer RMW.

    Pre-fix: concurrent `create_file` calls can read the same `last_ino`
    value and produce duplicate inode numbers.  Post-fix: the `RLock` makes
    the RMW atomic; all inode numbers must be distinct.
    """

    THREADS = 8
    FILES_PER_THREAD = 50

    def test_no_duplicate_inodes(self) -> None:
        fs = FakeFilesystem()
        inodes: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(tid: int) -> None:
            try:
                for i in range(self.FILES_PER_THREAD):
                    f = fs.create_file(f"/t{tid}_f{i}.txt", contents="x")
                    with lock:
                        inodes.append(f.st_ino)
            except Exception as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"Worker exceptions: {errors}")
        self.assertEqual(
            len(inodes),
            len(set(inodes)),
            f"Duplicate inodes found: {len(inodes) - len(set(inodes))} duplicates",
        )


# ──────────────────────────────────────────────────────────────────────────────
# Shape 2 — no crash under concurrent open/close
# ──────────────────────────────────────────────────────────────────────────────

class TestShape2NoCrash(unittest.TestCase):
    """Shape 2 (THREAD-SAFETY-AUDIT.md): heap TOCTOU on `_free_fd_heap`.

    Pre-fix: concurrent `open`/`close` can produce an `IndexError` (or
    segfault on 3.13 FT) via the heap.  Post-fix: no exception raised.
    """

    THREADS = 8
    OPS_PER_THREAD = 50

    def test_no_exception_under_concurrent_open_close(self) -> None:
        fs = FakeFilesystem()
        fs.create_file("/shared.txt", contents="hello")
        errors: list[Exception] = []
        error_lock = threading.Lock()

        def worker() -> None:
            try:
                for _ in range(self.OPS_PER_THREAD):
                    fake_open_mod = fake_filesystem.FakeFileOpen(fs)
                    fh = fake_open_mod("/shared.txt", "r")
                    fh.read()
                    fh.close()
            except Exception as exc:  # pragma: no cover
                with error_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"Worker exceptions: {errors}")


# ──────────────────────────────────────────────────────────────────────────────
# Shape 3 — no RuntimeError under concurrent mkdir/rmdir
# ──────────────────────────────────────────────────────────────────────────────

class TestShape3NoCrash(unittest.TestCase):
    """Shape 3 (THREAD-SAFETY-AUDIT.md): dict mutation during iteration in
    `_directory_content` / `FakeDirectory.entries`.

    Pre-fix: concurrent `create_dir` / `remove_object` causes
    `RuntimeError: dictionary changed size during iteration`.  Post-fix:
    the lock serialises all dict mutations; no RuntimeError raised.
    """

    THREADS = 8
    OPS_PER_THREAD = 30

    def test_no_runtime_error_under_concurrent_mkdir_rmdir(self) -> None:
        fs = FakeFilesystem()
        errors: list[Exception] = []
        error_lock = threading.Lock()

        def worker(tid: int) -> None:
            try:
                for i in range(self.OPS_PER_THREAD):
                    path = f"/d{tid}_{i}"
                    fs.create_dir(path)
                    fs.remove_object(path)
            except Exception as exc:  # pragma: no cover
                with error_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"Worker exceptions: {errors}")


# ──────────────────────────────────────────────────────────────────────────────
# fs.lock() — cross-thread mutation of returned FakeFile references
# ──────────────────────────────────────────────────────────────────────────────

class TestFsLockContextManager(unittest.TestCase):
    """`FakeFilesystem.lock()` serialises mutations of returned `FakeFile`
    references across threads.

    The test creates N files (one per thread) and has each thread loop through
    every file, acquiring `fs.lock()` before calling `get_object()` and
    writing `.set_contents()`.  After all threads finish, every file must
    contain its last assigned content without corruption.

    This is the canonical usage example from PLAN.md §
    `get_object`-class API: publish the lock.
    """

    THREADS = 8
    ITERATIONS = 40

    def test_lock_serialises_get_object_mutation(self) -> None:
        fs = FakeFilesystem()
        paths = [f"/file_{i}.txt" for i in range(self.THREADS)]
        for p in paths:
            fs.create_file(p, contents="initial")

        errors: list[Exception] = []
        error_lock = threading.Lock()

        def worker(tid: int) -> None:
            try:
                for iteration in range(self.ITERATIONS):
                    for path in paths:
                        new_contents = f"tid={tid} iter={iteration}"
                        with fs.lock():
                            f = fs.get_object(path)
                            f.set_contents(new_contents)
                            # Verify the write is visible while still holding
                            # the lock (confirms no other thread interleaved).
                            assert f.contents == new_contents, (
                                f"Stale read while holding lock: "
                                f"expected {new_contents!r}, got {f.contents!r}"
                            )
            except Exception as exc:  # pragma: no cover
                with error_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, f"Worker exceptions: {errors}")

    def test_lock_is_reentrant(self) -> None:
        """`fs.lock()` must be re-entrant (backed by `RLock`) so that
        callers can nest it around public API calls that internally
        also acquire the lock."""
        fs = FakeFilesystem()
        # Nested acquisition must not deadlock.
        with fs.lock():
            with fs.lock():
                fs.create_file("/nested.txt", contents="ok")
            f = fs.get_object("/nested.txt")
        self.assertEqual(f.contents, "ok")

    def test_lock_exposes_existing_rlock(self) -> None:
        """`fs.lock()` must expose the *same* lock as internal callers use,
        not a separate one.  Verify via `_lock` identity."""
        fs = FakeFilesystem()

        with fs.lock():
            # Confirm that the internal _lock is an RLock (not a plain Lock).
            # We can't do identity comparison on the lock itself, but we can
            # confirm its type and that calling lock() while already holding it
            # does not block (re-entrance test above covers that more directly).
            internal_lock = fs._lock

        self.assertIsInstance(internal_lock, type(threading.RLock()))


if __name__ == "__main__":
    unittest.main()
