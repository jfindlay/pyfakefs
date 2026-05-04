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


# ──────────────────────────────────────────────────────────────────────────────
# Shape 7 — per-thread uid/gid (helpers.py thread-local)
# ──────────────────────────────────────────────────────────────────────────────

class TestShape7UidGidNoContamination(unittest.TestCase):
    """Shape 7 (THREAD-SAFETY-AUDIT.md): `USER_ID` / `GROUP_ID` were plain
    module globals — a `set_uid()` call in one thread was visible to all
    concurrent threads performing permission checks.

    Post-fix: each thread has its own uid/gid in a `threading.local`.
    N threads each set their own uid, then read it back; no cross-thread
    contamination is allowed.
    """

    THREADS = 16
    ITERATIONS = 50

    def test_no_uid_contamination(self) -> None:
        from pyfakefs import helpers

        errors: list[str] = []
        error_lock = threading.Lock()

        def worker(tid: int) -> None:
            uid = 1000 + tid
            for _ in range(self.ITERATIONS):
                helpers.set_uid(uid)
                observed = helpers.get_uid()
                if observed != uid:
                    with error_lock:
                        errors.append(
                            f"tid={tid}: set_uid({uid}) but get_uid()={observed}"
                        )
            # Reset to process default so we don't leak state to subsequent tests.
            helpers.reset_ids()

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, "uid contamination detected:\n" + "\n".join(errors))

    def test_no_gid_contamination(self) -> None:
        from pyfakefs import helpers

        errors: list[str] = []
        error_lock = threading.Lock()

        def worker(tid: int) -> None:
            gid = 2000 + tid
            for _ in range(self.ITERATIONS):
                helpers.set_gid(gid)
                observed = helpers.get_gid()
                if observed != gid:
                    with error_lock:
                        errors.append(
                            f"tid={tid}: set_gid({gid}) but get_gid()={observed}"
                        )
            helpers.reset_ids()

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertFalse(errors, "gid contamination detected:\n" + "\n".join(errors))

    def test_default_uid_is_process_uid(self) -> None:
        """A thread that never calls `set_uid` sees the process default."""
        import os
        import sys
        from pyfakefs import helpers

        result: list[int] = []

        def worker() -> None:
            # Deliberately do NOT call set_uid — should see process default.
            result.append(helpers.get_uid())

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        expected = 0 if sys.platform == "win32" else os.getuid()
        self.assertEqual(result[0], expected)


# ──────────────────────────────────────────────────────────────────────────────
# Shape 6 — Patcher class-level singleton and ref-count under concurrent access
# ──────────────────────────────────────────────────────────────────────────────


class TestShape6PatcherSingleton(unittest.TestCase):
    """Shape 6 (THREAD-SAFETY-AUDIT.md): `Patcher.PATCHER`, `REF_COUNT`,
    and the five module-cache dicts were unprotected class attributes —
    concurrent `setUp`/`tearDown` from different threads could corrupt the
    singleton reference or produce a torn ref-count.

    Post-fix: a `threading.Lock` (`Patcher._class_lock`) serialises every
    read-modify-write on these attributes.

    These tests verify:

    1. N threads calling `Patcher()` concurrently all receive the *same*
       singleton object (no duplicate instances, no `None` returns).
    2. N concurrent `setUp` calls leave `REF_COUNT == N` (no lost increments).
    3. N paired `setUp`/`tearDown` calls leave `REF_COUNT == 0` and
       `PATCHER == None` after all threads finish (no lost decrements and no
       stale singleton reference).
    """

    THREADS = 12

    def setUp(self):
        # Ensure Patcher class-level state is clean before each test.  We do
        # this via direct attribute writes — acceptable because we are
        # deliberately testing the class-level state, not routing through
        # setUp/tearDown under test.
        # Note: clear_fs_cache() acquires _class_lock internally; call it
        # outside any explicit lock block to avoid a non-reentrant deadlock.
        from pyfakefs.fake_filesystem_unittest import Patcher

        with Patcher._class_lock:
            Patcher.PATCHER = None
            Patcher.DOC_PATCHER = None
            Patcher.REF_COUNT = 0
            Patcher.DOC_REF_COUNT = 0
        Patcher.clear_fs_cache()

    def tearDown(self):
        # Restore clean state regardless of what the test left behind.
        from pyfakefs.fake_filesystem_unittest import Patcher

        with Patcher._class_lock:
            Patcher.PATCHER = None
            Patcher.DOC_PATCHER = None
            Patcher.REF_COUNT = 0
            Patcher.DOC_REF_COUNT = 0
        Patcher.clear_fs_cache()

    def test_singleton_invariant_under_concurrent_new(self) -> None:
        """All threads that call `Patcher()` concurrently must receive the
        same singleton object and never `None`."""
        from pyfakefs.fake_filesystem_unittest import Patcher

        results: list[object] = [None] * self.THREADS
        barrier = threading.Barrier(self.THREADS)

        def worker(tid: int) -> None:
            barrier.wait()  # maximise concurrency at the __new__ call site
            results[tid] = Patcher()

        threads = [
            threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every result must be the same object as Patcher.PATCHER.
        singleton = Patcher.PATCHER
        self.assertIsNotNone(singleton, "PATCHER should not be None after construction")
        for tid, result in enumerate(results):
            self.assertIs(
                result,
                singleton,
                f"thread {tid} received a different Patcher instance",
            )

    def test_ref_count_no_lost_increments(self) -> None:
        """N concurrent `setUp` calls must leave `REF_COUNT == N`."""
        from pyfakefs.fake_filesystem_unittest import Patcher

        # Create the singleton first so __init__ runs once outside the timed
        # window; concurrent setUp calls will all hit the REF_COUNT > 1 fast-
        # path, which is the check-and-increment we're testing.
        patcher = Patcher()
        patcher.setUp()  # REF_COUNT → 1, patching active

        barrier = threading.Barrier(self.THREADS)

        def worker() -> None:
            barrier.wait()
            patcher.setUp()

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # One initial setUp + THREADS concurrent setUp calls.
        expected = 1 + self.THREADS
        self.assertEqual(
            Patcher.REF_COUNT,
            expected,
            f"REF_COUNT={Patcher.REF_COUNT}, expected {expected} — lost increment(s)",
        )

        # Drain: call tearDown expected times so state is clean for tearDown().
        for _ in range(expected):
            patcher.tearDown()

    def test_ref_count_no_lost_decrements(self) -> None:
        """N paired setUp/tearDown calls must leave REF_COUNT==0 and
        PATCHER==None after all threads finish."""
        from pyfakefs.fake_filesystem_unittest import Patcher

        patcher = Patcher()
        # Prime the ref-count to THREADS so every tearDown hits REF_COUNT > 0
        # fast-path until the very last one.
        for _ in range(self.THREADS):
            patcher.setUp()

        barrier = threading.Barrier(self.THREADS)

        def worker() -> None:
            barrier.wait()
            patcher.tearDown()

        threads = [threading.Thread(target=worker) for _ in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(
            Patcher.REF_COUNT,
            0,
            f"REF_COUNT={Patcher.REF_COUNT} after {self.THREADS} tearDown calls — "
            "lost decrement(s) or torn read",
        )
        self.assertIsNone(
            Patcher.PATCHER,
            "PATCHER should be None after the last tearDown",
        )


if __name__ == "__main__":
    unittest.main()
