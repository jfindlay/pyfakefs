"""
Pass C MRE — Shape 3: Dict mutate-during-iterate in _directory_content

Hazard:   pyfakefs/fake_filesystem.py:1523-1526
Shape:    Shape 3 — dict mutate-during-iterate (THREAD-SAFETY-AUDIT.md)
Observed: RuntimeError: dictionary changed size during iteration

Pre-fix observation:
    PyPy 7.3.19 / Python 3.11 (x86_64 Linux): 940/50 repeats crashed.
    CPython 3.13 GIL-disabled: 620/640 workers crashed.

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises _directory_content (called inside
    lock-wrapped FakeFilesystem methods) and add_entry.  Expected: 0 crashes.
    Exits 0 post-fix.

Note: This hazard fires only on a case-insensitive filesystem because the
listcomp at line 1523 is only reached when is_case_sensitive=False.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape3_dict_mutate.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape3_dict_mutate.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape3_dict_mutate.py

Background:
    _directory_content (fake_filesystem.py:1515) contains:

        matching_content = [
            (subdir, directory.entries[subdir])   # line 1523
            for subdir in directory.entries        # <--- iterates entries
            if subdir.lower() == component.lower()
        ]

    This listcomp iterates FakeDirectory._entries while add_entry
    (fake_file.py:563) mutates it in another thread.

    Sibling sites (same shape, different dict):
      - fake_file.py:594  (_normalized_entryname iterates self.entries)
      - fake_filesystem.py:568-576, 630-639, 659-669, 673, 1015, 1464, 1479
        (mount_points iteration sites)
"""
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from pyfakefs.fake_filesystem import FakeFilesystem

THREADS = 32
N_PER = 64
REPEATS = 20


def mkdir_worker(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Create n directories under /shared, triggering _directory_content
    (existence check) concurrent with add_entry mutations.

    :param fs: Active FakeFilesystem instance (case-insensitive).
    :param n: Number of mkdir operations.
    :param prefix: Per-thread distinguishing prefix to avoid name collisions.
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.create_dir(f"/shared/d_{prefix}_{tid}_{i}")
        except RuntimeError:
            raise  # re-raise; this is the hazard
        except OSError:
            pass  # benign: race on existence check (Shape 5), not our hazard


def main() -> None:
    """Run the MRE and print all captured RuntimeErrors."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 3 (dict mutate-during-iterate) MRE — {interpreter} — {gil_status}")
    print(f"Filesystem: case-insensitive (required to reach line 1523 listcomp)")

    failures: list[tuple[type, str]] = []
    lock = threading.Lock()
    total_crashes = 0

    for repeat in range(REPEATS):
        fs = FakeFilesystem("/")
        fs.is_case_sensitive = False
        fs.create_dir("/shared")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [
                pool.submit(mkdir_worker, fs, N_PER, i)
                for i in range(THREADS)
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

    # Post-fix assertion: RLock serialises dict iteration and mutation; no crashes.
    if failures:
        print(f"\n=== First RuntimeError ===")
        print(failures[0][1])
        print("REGRESSION: dict mutate-during-iterate produced a crash.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no crashes (post-fix invariant holds).")


if __name__ == "__main__":
    main()
