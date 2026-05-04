"""
Pass C MRE — Shape 3+4: open_files listcomp and allocator race

Hazards:
  - fake_filesystem.py:1015 (Shape 3.9 — has_open_file listcomp)
  - fake_filesystem.py:951-952 (Shape 4.1 — append + len-1 allocator)
  - fake_file.py:987, 1177 (Shape 3.11+8.1 — open_files[3:] with nullification)

Shape:    Shape 3 (dict/list iter-during-mutate) + Shape 4 (allocator race)
Observed:
  3.9: IndexError or wrong result from has_open_file when open_files mutates mid-listcomp
  4.1: Duplicate fd numbers returned by add_open_file (silent data race)
  3.11/8.1: IndexError or stale data from open_files[3:] iteration while
            close_open_file sets elements to None

Pre-fix observation:
    PyPy + CPython 3.13 FT: crashes in 3/20 and 11/20 repeats respectively.

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises all open_files access (add_open_file,
    close_open_file, has_open_file, and flush helpers).  Expected: 0 crashes.
    Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape3_open_files.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape3_open_files.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape3_open_files.py

Background:
    has_open_file (fake_filesystem.py:1014-1016):
        return file_object in [
            wrappers[0].get_object()
            for wrappers in self.open_files if wrappers  # iterates while add/close mutate
        ]

    add_open_file (fake_filesystem.py:951-952):
        self.open_files.append([file_obj])
        return len(self.open_files) - 1  # race: two threads compute same index
"""
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
import os

THREADS = 16
N_PER = 64
REPEATS = 20


def open_close_worker(n: int) -> None:
    """Open and close n files; stresses add_open_file (allocator + listcomp).

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


def check_open_worker(n: int) -> None:
    """Call os.stat on shared paths; exercises the open_files listcomp.

    :param n: Number of stat calls.
    """
    for _ in range(n):
        try:
            os.stat("/shared")
        except OSError:
            pass


def main() -> None:
    """Run concurrent open/close + stat workers; report crashes."""
    from pyfakefs.fake_filesystem_unittest import Patcher

    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 3.9+4.1+3.11+8.1 (open_files races) MRE — {interpreter} — {gil_status}")

    crashes = 0
    first_tb: str = ""

    for _repeat in range(REPEATS):
        with Patcher() as p:
            assert p.fs is not None
            p.fs.create_dir("/shared")

            with ThreadPoolExecutor(max_workers=THREADS) as pool:
                oc_futures = [
                    pool.submit(open_close_worker, N_PER)
                    for _ in range(THREADS // 2)
                ]
                st_futures = [
                    pool.submit(check_open_worker, N_PER)
                    for _ in range(THREADS // 2)
                ]
                for f in oc_futures + st_futures:
                    try:
                        f.result()
                    except Exception:
                        if not first_tb:
                            first_tb = traceback.format_exc()
                        crashes += 1

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Crashes: {crashes}")

    # Post-fix assertion: RLock serialises all open_files access; no crashes.
    if crashes:
        print(f"\n=== First crash ===")
        print(first_tb)
        print("REGRESSION: open_files race produced a crash.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no crashes (post-fix invariant holds).")


if __name__ == "__main__":
    main()
