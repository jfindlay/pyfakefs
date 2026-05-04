"""
Pass C MRE — Shape 1+3: Integer RMW and dict iteration in add_mount_point

Hazards:
  - fake_filesystem.py:568 (Shape 3.5 — mount_points iter-during-mutate)
  - fake_filesystem.py:579 (Shape 1.1 — last_dev RMW)
  - fake_filesystem.py:588 (Shape 1.2 — last_ino RMW)

Shape:    Shape 1 + Shape 3 (THREAD-SAFETY-AUDIT.md)
Observed: RuntimeError: OrderedDict mutated during iteration (Shape 3.5, crashes)
          Silent: duplicate idev values across mount points (Shape 1.1)

Pre-fix observation:
    Shape 3.5 confirmed on both PyPy and CPython 3.13 FT (fires on first repeat).
    Shape 1.1 (last_dev): masked by Shape 3.5 crash firing first.

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises both the dict iteration and the RMW
    counters inside add_mount_point.  Expected: 0 crashes, 0 idev duplicates.
    Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape1_mount_point.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape1_mount_point.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape1_mount_point.py

Background:
    add_mount_point (fake_filesystem.py:565-593) first iterates mount_points
    to check for duplicates (line 568), then increments last_dev (line 579).
    Concurrent calls race on the iteration before reaching the RMW, so Shape 3.5
    is the dominant failure mode.
"""
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from pyfakefs.fake_filesystem import FakeFilesystem

THREADS = 16
N_PER = 4    # fewer ops: each creates a new mount point
REPEATS = 20


def mount_burst(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Create n distinct mount points concurrently, each triggering last_dev +=.

    :param fs: Active FakeFilesystem instance (shared across threads).
    :param n: Number of mount points to create.
    :param prefix: Per-invocation distinguishing prefix.
    :raises RuntimeError: if Shape 3.5 fires (dict mutated during iteration).
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.add_mount_point(f"/mnt/t{prefix}_{tid}_{i}")
        except OSError:
            pass  # benign: race on existence check (Shape 5)
        # RuntimeError (Shape 3.5 — dict iter) propagates to f.result() for counting.


def main() -> None:
    """Run the MRE and check for duplicate device IDs across mount points."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 1.1+1.2 (add_mount_point last_dev/last_ino) MRE — {interpreter} — {gil_status}")

    crash_repeats = 0
    violation_repeats = 0
    first_tb: str = ""

    for _repeat in range(REPEATS):
        fs = FakeFilesystem("/")
        fs.create_dir("/mnt")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [
                pool.submit(mount_burst, fs, N_PER, i)
                for i in range(THREADS)
            ]
            for f in futures:
                try:
                    f.result()
                except RuntimeError:
                    import traceback as _tb
                    if not first_tb:
                        first_tb = _tb.format_exc()
                    crash_repeats += 1
                    break  # one crash per repeat is enough to count

        # Invariant: every mount point must have a unique idev.
        idevs = [mp["idev"] for mp in fs.mount_points.values()]
        dup_devs = [d for d, c in Counter(idevs).items() if c > 1]
        if dup_devs:
            violation_repeats += 1

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Shape 3.5 crashes (dict iter on mount_points): {crash_repeats}/{REPEATS}")
    print(f"Shape 1.1 violations (duplicate idev):         {violation_repeats}/{REPEATS}")

    # Post-fix assertion: both hazards must be zero.
    if crash_repeats or violation_repeats:
        if crash_repeats:
            print(f"\n=== Shape 3.5 RuntimeError ===")
            print(first_tb)
        if violation_repeats:
            print("\nREGRESSION: last_dev RMW produced duplicate idev values.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no crashes or idev duplicates (post-fix invariant holds).")


if __name__ == "__main__":
    main()
