"""
Pass C MRE — Shape 6: Class-attribute toggle races

Hazards:
  - fake_path.py:122-126  (FakePathModule.reset writes cls.sep etc.)
  - fake_filesystem_unittest.py (Patcher._class_lock sites — see Phase 3 commit)
  - fake_filesystem_unittest.py:1040-1044  (setUp increments cls.REF_COUNT)
  - fake_filesystem_unittest.py:1160-1164  (tearDown decrements cls.REF_COUNT)
  - fake_filesystem_unittest.py:605, 608  (__new__ checks cls.PATCHER is None then assigns)

Shape:    Shape 6 — class-attribute toggle (THREAD-SAFETY-AUDIT.md)
Observed: Silent — wrong sep in os.path calls; premature teardown; dual Patcher instances

Pre-fix observation:
    CPython 3.13 GIL-disabled: KeyError/NameError in tearDown (20/20 repeats).
    Silent patcher leaks observed when PATCHER not cleaned up after races.

Post-fix — two fixes landed:
    Phase 2 (commit 891b6cb): FakePathModule.sep/altsep/linesep/devnull/pathsep
    converted from class attributes to per-instance properties; sep contamination
    between threads eliminated.

    Phase 3 (commit fde6aba): Patcher._class_lock (threading.Lock) serialises
    __new__ (singleton check+assign), setUp/tearDown (REF_COUNT check+increment/
    decrement), and clear_fs_cache (5 cache dicts atomically replaced).

    The unit tests in test_threading.py::TestShape6PatcherSingleton cover these
    invariants deterministically.  This MRE is kept as a standalone executable
    for interpreter-specific regression checks.

Expected post-fix: 0 crashes, 0 patcher leaks.  Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape6_class_attrs.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape6_class_attrs.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape6_class_attrs.py
"""
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

THREADS = 16
REPEATS = 20


def patcher_new_worker() -> None:
    """Construct a Patcher on multiple concurrent threads.

    Each call exercises the __new__ singleton guard and the REF_COUNT
    increment in setUp.

    :raises Exception: if any unexpected error occurs during Patcher construction.
    """
    from pyfakefs.fake_filesystem_unittest import Patcher
    p = Patcher()
    p.setUp()
    try:
        import os
        os.stat("/")  # exercise patched os
    finally:
        p.tearDown()


def sep_reader_worker(n: int) -> None:
    """Read os.path.sep n times while other threads may call FakePathModule.reset.

    :param n: Number of reads.
    """
    import os
    for _ in range(n):
        _ = os.path.sep  # should always be '/' or '\\', never empty or None


def main() -> None:
    """Run concurrent Patcher constructions and report anomalies."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 6 (class-attr toggle races) MRE — {interpreter} — {gil_status}")

    crashes = 0
    patcher_leaks = 0
    first_tb: str = ""

    for _repeat in range(REPEATS):
        from pyfakefs.fake_filesystem_unittest import Patcher
        # Reset the singleton state before each repeat so the race can fire.
        with Patcher._class_lock:
            Patcher.PATCHER = None
            Patcher.REF_COUNT = 0

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [pool.submit(patcher_new_worker) for _ in range(THREADS)]
            for f in futures:
                try:
                    f.result()
                except Exception:
                    if not first_tb:
                        first_tb = traceback.format_exc()
                    crashes += 1

        # If PATCHER is not None and REF_COUNT != 0, tearDown didn't clean up.
        if Patcher.PATCHER is not None and Patcher.REF_COUNT != 0:
            patcher_leaks += 1
        # Reset for next repeat
        with Patcher._class_lock:
            Patcher.PATCHER = None
            Patcher.REF_COUNT = 0

    print(f"Repeats: {REPEATS}  Threads: {THREADS}")
    print(f"Crashes: {crashes}/{REPEATS}")
    print(f"Patcher leaks (PATCHER not cleaned up): {patcher_leaks}/{REPEATS}")

    # Post-fix assertion: _class_lock serialises singleton and REF_COUNT; no crashes or leaks.
    if crashes or patcher_leaks:
        if crashes:
            print(f"\n=== First crash ===")
            print(first_tb)
        print("REGRESSION: Shape 6 class-attr race produced a crash or patcher leak.")
        print("Patcher._class_lock must guard __new__, setUp, tearDown, and clear_fs_cache.")
        sys.exit(1)
    else:
        print("OK — no crashes or patcher leaks (post-fix invariant holds).")


if __name__ == "__main__":
    main()
