"""
Pass C MRE — Shape 2: Heap TOCTOU in add_open_file / close_open_file

Hazard:   pyfakefs/fake_filesystem.py:946-948, 965
Shape:    Shape 2 — heap TOCTOU (THREAD-SAFETY-AUDIT.md)
Observed: IndexError: list index out of range
          Two crash paths confirmed:
            (a) add_open_file: heappop after concurrent thread drains heap
            (b) close_open_file: heappush corrupts heap mid-siftdown

Pre-fix observation:
    PyPy 7.3.19 / Python 3.11 (arm64 macOS + x86_64 Linux): fires reliably
    within seconds.  CPython 3.13 GIL-disabled: fires on most repeats.

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises add_open_file and close_open_file,
    including the heappop/heappush pair.  Expected: 0 crashes.
    Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape2_heap_toctou.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape2_heap_toctou.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape2_heap_toctou.py

Background:
    FakeFilesystem._free_fd_heap is a plain list used as a heapq.  Two racing
    operations exist:

    Path (a) — add_open_file (fake_filesystem.py:946-948):
        if self._free_fd_heap:          # check
            open_fd = heapq.heappop(...)  # pop — another thread may have
                                           # emptied the heap between check and pop

    Path (b) — close_open_file (fake_filesystem.py:965):
        heapq.heappush(self._free_fd_heap, file_des)
        # concurrent heappush calls interleave inside _siftdown, producing
        # an inconsistent heap state that raises IndexError on the next access
"""
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
import os

THREADS = 32
N_PER = 64
REPEATS = 20


def open_close_worker(n: int) -> None:
    """Open and close n unique files; both open (add_open_file) and close
    (close_open_file) touch the shared heap.

    :param n: Number of open/close cycles.
    """
    tid = threading.get_ident()
    for i in range(n):
        path = f"/shared/f_{tid}_{i}"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        except (OSError, IndexError):
            raise


def main() -> None:
    """Run the MRE and print all captured failure tracebacks."""
    from pyfakefs.fake_filesystem_unittest import Patcher

    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 2 (heap TOCTOU) MRE — {interpreter} — {gil_status}")

    failures: list[tuple[type, str]] = []
    lock = threading.Lock()
    total_crashes = 0

    for repeat in range(REPEATS):
        with Patcher() as p:
            assert p.fs is not None
            p.fs.create_dir("/shared")
            with ThreadPoolExecutor(max_workers=THREADS) as pool:
                futures = [
                    pool.submit(open_close_worker, N_PER)
                    for _ in range(THREADS)
                ]
                for f in futures:
                    try:
                        f.result()
                    except Exception as exc:
                        total_crashes += 1
                        with lock:
                            failures.append((type(exc), traceback.format_exc()))

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Total crashes: {total_crashes}")

    # Post-fix assertion: RLock serialises heap access; no crashes must occur.
    if failures:
        seen_types: set[str] = set()
        for exc_type, tb in failures:
            key = exc_type.__name__
            if key not in seen_types:
                seen_types.add(key)
                print(f"\n=== {exc_type.__name__} (first occurrence) ===")
                print(tb)
        print("REGRESSION: heap TOCTOU produced a crash.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no crashes (post-fix invariant holds).")


if __name__ == "__main__":
    main()
