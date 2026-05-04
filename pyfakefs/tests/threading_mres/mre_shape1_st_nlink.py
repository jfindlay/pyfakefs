"""
Pass C MRE — Shape 1: st_nlink RMW in add_entry / remove_entry

Hazards:  fake_file.py:568 (st_nlink += in add_entry)
          fake_file.py:569 (path_object.st_nlink += in add_entry)
          fake_file.py:642 (st_nlink -= in remove_entry)
          fake_file.py:643 (entry.st_nlink -= in remove_entry)
Shape:    Shape 1 — integer read-modify-write counter (THREAD-SAFETY-AUDIT.md)
Observed: Silent invariant violation — wrong link counts; assertion error on
          underflow under free-threaded CPython

Pre-fix observation:
    Fires alongside mre_shape1_last_ino.py (same add_entry call path).
    Dedicated MRE to isolate st_nlink specifically.

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises add_entry and remove_entry.
    Expected: 0 crashes, 0 link-count violations.  Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape1_st_nlink.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape1_st_nlink.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape1_st_nlink.py

Background:
    add_entry (fake_file.py:563-574):
        self.st_nlink += 1          # parent link count — line 568
        path_object.st_nlink += 1   # child link count — line 569

    remove_entry (fake_file.py:642-643):
        self.st_nlink -= 1
        entry.st_nlink -= 1

    Concurrent add + remove on the same directory can produce a final
    st_nlink != expected value, or trigger the assert at line 644
    (assert entry.st_nlink >= 0) under free-threaded CPython.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from pyfakefs.fake_filesystem import FakeFilesystem

THREADS = 16
N_PER = 32
REPEATS = 10


def add_remove_worker(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Concurrently create and remove files in /shared.

    Each create calls add_entry (st_nlink +=); each remove calls remove_entry
    (st_nlink -=).  Races on both the parent dir's link count and the file's
    own link count.

    :param fs: Active FakeFilesystem instance (shared across threads).
    :param n: Number of create+delete cycles.
    :param prefix: Per-invocation distinguishing prefix.
    """
    tid = threading.get_ident()
    for i in range(n):
        path = f"/shared/f_{prefix}_{tid}_{i}"
        try:
            fs.create_file(path, contents="x")
        except OSError:
            pass
        try:
            fs.remove_object(path)
        except OSError:
            pass


def main() -> None:
    """Run the MRE and check for assertion errors or wrong link counts."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 1.4+1.5 (st_nlink RMW) MRE — {interpreter} — {gil_status}")

    crash_repeats = 0
    violation_repeats = 0
    import traceback as _tb
    first_tb: str = ""

    for _repeat in range(REPEATS):
        fs = FakeFilesystem("/")
        fs.create_dir("/shared")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [
                pool.submit(add_remove_worker, fs, N_PER, i)
                for i in range(THREADS)
            ]
            for f in futures:
                try:
                    f.result()
                except (AssertionError, Exception) as exc:
                    if not first_tb:
                        first_tb = _tb.format_exc()
                    crash_repeats += 1

        # After all adds and removes /shared should have st_nlink >= 2.
        shared = fs.get_object("/shared")
        if shared.st_nlink < 2:
            violation_repeats += 1

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Crashes (AssertionError / unexpected exc): {crash_repeats}/{REPEATS}")
    print(f"Invariant violations (st_nlink < 2):       {violation_repeats}/{REPEATS}")

    # Post-fix assertion: RLock serialises add/remove_entry; both must be zero.
    if crash_repeats or violation_repeats:
        if crash_repeats:
            print(f"\n=== First crash ===")
            print(first_tb)
        print("REGRESSION: st_nlink RMW produced wrong link counts.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no crashes or st_nlink violations (post-fix invariant holds).")


if __name__ == "__main__":
    main()
