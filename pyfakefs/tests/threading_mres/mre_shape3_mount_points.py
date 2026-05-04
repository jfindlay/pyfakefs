"""
Pass C MRE — Shape 3: Dict mutate-during-iterate on mount_points

Hazards:
  - fake_filesystem.py:568-576  (add_mount_point iteration — Shape 3.5)
  - fake_filesystem.py:630-639  (_mount_point_for_path — Shape 3.6)
  - fake_filesystem.py:659-669  (_mount_point_dir_for_cwd — Shape 3.7)
  - fake_filesystem.py:673      (_mount_point_for_device — Shape 3.8)
  - fake_filesystem.py:1464     (replace_windows_root — Shape 3.10a)
  - fake_filesystem.py:1479     (is_mount_point — Shape 3.10b)

Shape:    Shape 3 — dict mutate-during-iterate (THREAD-SAFETY-AUDIT.md)
Observed: RuntimeError: dictionary changed size during iteration

Pre-fix observation:
    Shape 3.5 confirmed: RuntimeError on both PyPy and CPython 3.13 FT
    (20/20 repeats on 3.13 FT; 2/20 on PyPy).

Post-fix (branch fix-thread-races, commit 613867f):
    Per-FakeFilesystem RLock serialises all six iteration sites and
    add_mount_point's insertion.  Expected: 0 crashes.  Exits 0 post-fix.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape3_mount_points.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape3_mount_points.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape3_mount_points.py

Background:
    self.mount_points is an OrderedDict iterated in six functions.  Any
    concurrent call to add_mount_point (which inserts into mount_points)
    races with any of the six iteration sites.
"""
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from pyfakefs.fake_filesystem import FakeFilesystem

THREADS = 8
N_PER = 32
REPEATS = 20


def writer(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Add n mount points concurrently — mutates mount_points dict.

    :param fs: Active FakeFilesystem instance.
    :param n: Number of mount points to add.
    :param prefix: Per-invocation distinguishing prefix.
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.add_mount_point(f"/mnt/w{prefix}_{tid}_{i}")
        except (OSError, RuntimeError):
            raise  # RuntimeError = Shape 3.5; propagate


def reader(fs: FakeFilesystem, n: int) -> None:
    """Call _mount_point_for_path n times — iterates mount_points dict.

    :param fs: Active FakeFilesystem instance.
    :param n: Number of read operations.
    :raises RuntimeError: if dict changes size during iteration.
    """
    for _ in range(n):
        try:
            fs._mount_point_for_path("/")
        except RuntimeError:
            raise  # Shape 3.6; propagate
        except (KeyError, OSError):
            pass


def main() -> None:
    """Run writers and readers concurrently; report any RuntimeErrors."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(
        f"Shape 3.5–3.10 (mount_points dict iter-during-mutate) MRE"
        f" — {interpreter} — {gil_status}"
    )

    crashes = 0
    first_tb: str = ""

    for _repeat in range(REPEATS):
        fs = FakeFilesystem("/")
        fs.create_dir("/mnt")

        half = THREADS // 2
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            w_futures = [pool.submit(writer, fs, N_PER, i) for i in range(half)]
            r_futures = [pool.submit(reader, fs, N_PER) for _ in range(THREADS - half)]
            for f in w_futures + r_futures:
                try:
                    f.result()
                except RuntimeError:
                    if not first_tb:
                        first_tb = traceback.format_exc()
                    crashes += 1
                except Exception:
                    pass

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"RuntimeError (dict changed size) crashes: {crashes}")

    # Post-fix assertion: RLock serialises all mount_points access; no crashes.
    if crashes:
        print(f"\n=== First RuntimeError ===")
        print(first_tb)
        print("REGRESSION: mount_points dict mutate-during-iterate produced a crash.")
        print("The per-FakeFilesystem RLock (_with_lock decorator) must be intact.")
        sys.exit(1)
    else:
        print("OK — no crashes (post-fix invariant holds).")


if __name__ == "__main__":
    main()
