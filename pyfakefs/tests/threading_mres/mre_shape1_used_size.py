"""
Pass C MRE — Shape 1: used_size RMW in change_disk_usage

Hazard:   fake_filesystem.py:751 (mount_point["used_size"] += usage_change)
Shape:    Shape 1 — integer read-modify-write counter (THREAD-SAFETY-AUDIT.md)
Observed: Silent invariant violation — wrong disk usage accounting

Pre-fix observation:
    CPython 3.13 GIL-disabled: 10/10 repeats had a used_size mismatch
    (max delta observed: 82 KB across 16 threads × 64 files × 1024 B each).

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises change_disk_usage.  The dict-subscript
    RMW (mount_point["used_size"] +=) is now atomic within the lock.
    Expected: 0 repeats with a mismatch.  Exits 0 post-fix.

Note: The hazard is a dict-subscript RMW (mount_point["used_size"] +=), so it
is not caught by the static-pattern check (which only matches attribute-style
RMW like self.attr +=).

Run:
    python  pyfakefs/tests/threading_mres/mre_shape1_used_size.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape1_used_size.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape1_used_size.py

Background:
    change_disk_usage (fake_filesystem.py:745-751):

        mount_point["used_size"] += usage_change    # line 751

    Concurrent file creates/removes each call change_disk_usage.  Under a race,
    the final used_size diverges from the sum of actual file sizes.
"""
import sys
from concurrent.futures import ThreadPoolExecutor

from pyfakefs.fake_filesystem import FakeFilesystem

THREADS = 16
N_PER = 64
REPEATS = 10
FILE_SIZE = 1024  # bytes per file


def create_worker(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Create n files with known content, each triggering change_disk_usage.

    :param fs: Active FakeFilesystem instance (shared across threads).
    :param n: Number of files to create.
    :param prefix: Per-invocation distinguishing prefix.
    """
    import threading
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.create_file(f"/shared/f_{prefix}_{tid}_{i}", contents="x" * FILE_SIZE)
        except OSError:
            pass


def main() -> None:
    """Run the MRE and compare reported used_size to the actual sum of sizes."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 1.6 (used_size RMW) MRE — {interpreter} — {gil_status}")

    violation_repeats = 0
    max_delta = 0

    for _repeat in range(REPEATS):
        fs = FakeFilesystem("/", total_size=THREADS * N_PER * FILE_SIZE * 2)
        fs.create_dir("/shared")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [
                pool.submit(create_worker, fs, N_PER, i)
                for i in range(THREADS)
            ]
            for f in futures:
                f.result()

        # Walk the filesystem and sum actual file sizes.
        actual_size = sum(
            fs.get_object(f"/shared/{name}").st_size
            for name in fs.get_object("/shared").entries
        )
        reported_size = fs.get_disk_usage()[1]  # index 1 = used
        delta = abs(reported_size - actual_size)
        if delta > 0:
            violation_repeats += 1
            max_delta = max(max_delta, delta)

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Repeats with used_size mismatch: {violation_repeats}/{REPEATS}")
    if violation_repeats:
        print(f"Max delta: {max_delta} bytes")

    # Post-fix assertion: RLock serialises change_disk_usage; delta must be 0.
    if violation_repeats:
        print("\nREGRESSION: used_size RMW produced wrong disk accounting.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — used_size matches actual file sizes (post-fix invariant holds).")


if __name__ == "__main__":
    main()
