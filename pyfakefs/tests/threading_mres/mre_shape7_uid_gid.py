"""
Pass C MRE — Shape 7: Process-global module variable race (USER_ID, GROUP_ID)

Hazard:   pyfakefs/helpers.py:83 (USER_ID = uid), helpers.py:99 (GROUP_ID = gid)
Shape:    Shape 7 — process-global module variable (THREAD-SAFETY-AUDIT.md)
Observed: Silent — permission checks see another thread's uid/gid

Pre-fix observation:
    CPython 3.13 GIL-disabled: 5/5 repeats had cross-thread UID contamination.

Post-fix (branch fix-thread-races, commit 891b6cb):
    helpers.USER_ID / GROUP_ID replaced with threading.local (_id_thread_state).
    set_uid/set_gid write to thread-local storage; get_uid/get_gid read from it
    (falling back to process defaults for threads that never called set_uid/set_gid).
    Expected: 0 repeats with contamination.  Exits 0 post-fix.

    The unit tests in test_threading.py::TestShape7UidGidNoContamination cover
    the same invariant deterministically.

Run:
    python  pyfakefs/tests/threading_mres/mre_shape7_uid_gid.py
    pypy3   pyfakefs/tests/threading_mres/mre_shape7_uid_gid.py
    python3.13t pyfakefs/tests/threading_mres/mre_shape7_uid_gid.py

Background:
    helpers.py:83:
        global USER_ID
        USER_ID = uid   # plain module-global write, no lock

    The race: Thread A calls set_uid(0) (root) to bypass permission checks.
    Thread B reads USER_ID for a permission check mid-way through Thread A's
    write.  Thread B may see Thread A's uid when it should see its own.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pyfakefs import helpers

THREADS = 16
N_PER = 10000
REPEATS = 5


def set_get_uid_worker(target_uid: int, n: int) -> list[int]:
    """Set USER_ID to target_uid then immediately read it back n times.

    :param target_uid: The UID this thread will set before each read.
    :param n: Number of set+get cycles.
    :returns: List of observed UID values (should all equal target_uid post-fix).
    """
    observed: list[int] = []
    for _ in range(n):
        helpers.set_uid(target_uid)
        observed.append(helpers.get_uid())
    helpers.reset_ids()  # restore process defaults for this thread
    return observed


def main() -> None:
    """Run concurrent set_uid workers with different target UIDs; detect cross-thread reads."""
    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"Shape 7 (USER_ID/GROUP_ID module globals) MRE — {interpreter} — {gil_status}")

    contamination_repeats = 0
    total_cross_reads = 0

    uids = list(range(THREADS))

    for _repeat in range(REPEATS):
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = {
                pool.submit(set_get_uid_worker, uid, N_PER): uid
                for uid in uids
            }
            cross_reads = 0
            for f, expected_uid in futures.items():
                observed = f.result()
                cross_reads += sum(1 for v in observed if v != expected_uid)
            if cross_reads:
                contamination_repeats += 1
                total_cross_reads += cross_reads

    print(f"Repeats: {REPEATS}  Threads: {THREADS}  Ops/thread: {N_PER}")
    print(f"Repeats with cross-thread UID contamination: {contamination_repeats}/{REPEATS}")
    if total_cross_reads:
        print(f"Total cross-thread reads: {total_cross_reads}")

    # Post-fix assertion: thread-local UID must never be contaminated.
    if contamination_repeats:
        print("\nREGRESSION: set_uid/get_uid produced cross-thread contamination.")
        print("helpers.USER_ID must be backed by threading.local (_id_thread_state).")
        sys.exit(1)
    else:
        print("OK — no UID contamination (post-fix invariant holds).")


if __name__ == "__main__":
    main()
