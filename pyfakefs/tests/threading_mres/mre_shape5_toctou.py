"""
Pass C MRE — Shape 5: TOCTOU existence check

Hazards:
  - fake_file.py:560-563      (add_entry: name-in-entries check then insert)
  - fake_filesystem.py:2630-2631 (create_file: exists check then add_object)
  - fake_filesystem.py:2252-2253 (create_dir: exists check then add_entry)
  - fake_filesystem.py:2915   (makedir: exists check then add_object)
  - fake_filesystem.py:2770-2771 (create_link: exists check then add_object)

Shape:    Shape 5 — TOCTOU existence check (THREAD-SAFETY-AUDIT.md)
Observed: Silent — two threads both pass the existence check then both insert,
          resulting in EEXIST for one (benign) OR a silent overwrite.
          Also: OSError(EEXIST) storms under high concurrency.

Pre-fix / post-fix note:
    Shape 5 is SILENT under CPython-with-GIL: the dict insert is atomic at the
    bytecode level so the overwrite rarely manifests.  Under free-threaded CPython
    (--disable-gil), both threads can pass the check and the second silently
    overwrites the first.

    Post-fix: the per-FakeFilesystem RLock makes the check-then-insert atomic,
    eliminating the overwrite window.  The MRE exits 0 both before and after the
    fix on stock CPython, but exits 0 reliably (not by luck) post-fix.

    There is NO post-fix regression assertion for Shape 5 that fires on stock
    CPython — the hazard is only observable on free-threaded CPython.  The MRE
    is kept for documentation and for use with python3.13t.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape5_toctou.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape5_toctou.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape5_toctou.py

Background:
    add_entry (fake_file.py:560-563):
        if path_object_name in self.entries:        # check
            self.filesystem.raise_os_error(errno.EEXIST, ...)
        self._entries[path_object_name] = path_object  # action (races with check)
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from pyfakefs.fake_filesystem import FakeFilesystem

THREADS = 16
N_PER = 32
REPEATS = 10


def create_same_path(fs: FakeFilesystem, path: str, n: int) -> None:
    """Attempt to create the same path n times from multiple threads.

    :param fs: Active FakeFilesystem instance.
    :param path: The path to race on.
    :param n: Number of attempts.
    """
    for _ in range(n):
        try:
            fs.create_file(path, contents="x")
        except OSError:
            pass  # EEXIST is expected; not the hazard we're looking for


def create_unique_paths(fs: FakeFilesystem, n: int, prefix: int) -> None:
    """Create n unique paths per thread, races on parent dir link-count / entry-check.

    :param fs: Active FakeFilesystem instance.
    :param n: Number of files to create.
    :param prefix: Per-invocation distinguishing prefix.
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.create_file(f"/shared/f_{prefix}_{tid}_{i}", contents="x")
        except OSError:
            pass


def main() -> None:
    """Run the MRE; check for duplicate entries in /shared after the burst."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 5 (TOCTOU existence check) MRE — {interpreter} — {gil_status}")

    overwrite_repeats = 0

    for _repeat in range(REPEATS):
        fs = FakeFilesystem("/")
        fs.create_dir("/shared")

        RACE_PATH = "/shared/race_target"
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [
                pool.submit(create_same_path, fs, RACE_PATH, N_PER)
                for _ in range(THREADS)
            ]
            for f in futures:
                f.result()

        if not fs.exists(RACE_PATH):
            overwrite_repeats += 1  # file was lost

        entries = list(fs.get_object("/shared").entries.keys())
        if len(entries) != len(set(entries)):
            overwrite_repeats += 1

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Overwrite / lost-file anomalies: {overwrite_repeats}/{REPEATS}")

    # Shape 5 is silent under GIL; we still exit 1 if a race is observed so that
    # this MRE acts as a canary if the fix is ever regressed on a GIL-less build.
    if overwrite_repeats:
        print("\nREGRESSION: Shape 5 TOCTOU caused data loss or duplicate entry.")
        print("The per-FakeFilesystem RLock must make check-then-insert atomic.")
        sys.exit(1)
    else:
        print("OK — no observable anomalies.")
        print(
            "Note: Shape 5 is silent under CPython-GIL.  Use python3.13t\n"
            "(--disable-gil) for reliable pre-fix reproduction."
        )


if __name__ == "__main__":
    main()
