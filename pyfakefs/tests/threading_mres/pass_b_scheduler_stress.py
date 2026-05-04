"""
Pass B — scheduler-amplifier stress workload for the pyfakefs thread-safety audit.

Run under each target interpreter:

    ~/pypy3/bin/pypy3 thread_safety_pass_b.py
    ~/.local/python3.13t/bin/python3.13t thread_safety_pass_b.py  # after build
    python3 thread_safety_pass_b.py                               # CPython baseline

Working document: THREAD-SAFETY-AUDIT.md § Pass B
Supporting issue: UMBRELLA-ISSUE-DRAFT.md
"""
import sys
import threading
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_failures: list[tuple[str, type, str]] = []
_failures_lock = threading.Lock()


def record_failure(primitive: str, exc: BaseException) -> None:
    """Record a worker crash into the shared failure list.

    :param primitive: Name of the primitive that raised.
    :param exc: The exception instance.
    """
    with _failures_lock:
        _failures.append((primitive, type(exc), traceback.format_exc()))


# ---------------------------------------------------------------------------
# Workload primitives
# Each takes (fs: Any, n: int) and performs n operations of one shape.
# fs is a FakeFilesystem instance; operations may call into patched builtins
# (open, os.*) since Patcher is active on the calling thread.
# ---------------------------------------------------------------------------

def mkdir_burst(fs: Any, n: int) -> None:
    """n concurrent mkdir under a shared parent.

    Stresses Shape 1 (st_nlink, last_ino) and Shape 3
    (_entries dict-mutate-during-iterate).

    :param fs: Active FakeFilesystem instance.
    :param n: Number of mkdir operations.
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.create_dir(f"/shared/t{tid}_{i}")
        except OSError:
            pass  # exist_ok equivalent; OSError expected on collision


def open_burst(fs: Any, n: int) -> None:
    """n open+close cycles against unique paths per thread.

    Stresses Shape 2 (_free_fd_heap TOCTOU) and Shape 4 (open_files allocator).

    :param fs: Active FakeFilesystem instance.
    :param n: Number of open/close cycles.
    """
    import os
    tid = threading.get_ident()
    for i in range(n):
        path = f"/shared/open_{tid}_{i}"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        except (OSError, KeyError, IndexError):
            pass


def stat_burst(fs: Any, n: int) -> None:
    """n os.stat calls against a pre-created path.

    Read-mostly; used as a noise generator to widen race windows for concurrent
    writers.

    :param fs: Active FakeFilesystem instance (unused directly; present for
        uniform primitive signature).
    :param n: Number of stat calls.
    """
    import os
    for _ in range(n):
        try:
            os.stat("/shared")
        except OSError:
            pass


def write_burst(fs: Any, n: int) -> None:
    """n create_file calls writing content.

    Stresses Shape 1 (used_size, epoch) and Shape 4 (open_files).

    :param fs: Active FakeFilesystem instance.
    :param n: Number of file creation operations.
    """
    tid = threading.get_ident()
    for i in range(n):
        try:
            fs.create_file(f"/shared/write_{tid}_{i}", contents="x" * 64)
        except OSError:
            pass


def link_burst(fs: Any, n: int) -> None:
    """n hard-link creation calls against a shared source.

    Stresses Shape 1 (st_nlink increment) and Shape 5 (link-create existence
    check).

    :param fs: Active FakeFilesystem instance (unused directly; os.link uses
        patched os).
    :param n: Number of link operations.
    """
    import os
    tid = threading.get_ident()
    for i in range(n):
        dst = f"/shared/link_{tid}_{i}"
        try:
            os.link("/shared/link_src", dst)
        except (OSError, FileExistsError):
            pass


def walk_while_mutate(fs: Any, n: int) -> None:
    """One os.walk against /shared concurrent with n mkdirs.

    Stresses Shape 3 (dict iter-during-mutate for mount_points and _entries).
    Threads are split by parity: even threads walk, odd threads create dirs.

    :param fs: Active FakeFilesystem instance.
    :param n: Number of mkdir operations (for odd threads).
    """
    import os
    tid = threading.get_ident()
    if tid % 2 == 0:
        try:
            for _ in os.walk("/shared"):
                pass
        except RuntimeError:
            raise
    else:
        for i in range(n):
            try:
                fs.create_dir(f"/shared/walk_{tid}_{i}")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------

def check_invariants(label: str) -> None:
    """Post-burst invariant assertions.

    A failure here is a silent data race (not a crash) per
    THREAD-SAFETY-AUDIT.md § Pass B outcome classifier.

    :param label: Descriptive label for the failure record.
    """
    import os

    # Collect all inodes; duplicates = Shape 1 last_ino race
    inodes: list[int] = []
    for root, dirs, files in os.walk("/"):
        for name in dirs + files:
            try:
                st = os.stat(os.path.join(root, name))
                inodes.append(st.st_ino)
            except OSError:
                pass
    if len(inodes) != len(set(inodes)):
        dups = [ino for ino, count in Counter(inodes).items() if count > 1]
        with _failures_lock:
            _failures.append((
                label,
                AssertionError,
                f"Duplicate inodes detected (Shape 1): {dups[:5]}",
            ))


# ---------------------------------------------------------------------------
# Workload combinator
# ---------------------------------------------------------------------------

def run_combo(
    primitive_name: str,
    primitive: Callable[[Any, int], None],
    patcher_factory: Callable[[], Any],
    threads: int = 8,
    n_per: int = 32,
    repeats: int = 10,
) -> dict[str, Any]:
    """Run one primitive under concurrent threads, repeated multiple times.

    Each repeat uses a fresh filesystem created by patcher_factory.

    :param primitive_name: Label for reporting.
    :param primitive: Workload function `(fs, n) -> None`.
    :param patcher_factory: Zero-arg callable returning a context manager that
        yields a Patcher (i.e. `Patcher` class itself).
    :param threads: Number of concurrent worker threads per repeat.
    :param n_per: Operations per worker thread.
    :param repeats: Number of independent repeat runs.
    :returns: Dict with keys primitive, repeats, crashes, invariant_violations.
    """
    crashes = 0
    failures_before = len(_failures)

    for repeat in range(repeats):
        with patcher_factory() as p:
            p.fs.create_dir("/shared")
            try:
                p.fs.create_file("/shared/link_src", contents="x")
            except OSError:
                pass

            with ThreadPoolExecutor(max_workers=threads) as pool:
                futures = [
                    pool.submit(primitive, p.fs, n_per) for _ in range(threads)
                ]
                for f in futures:
                    try:
                        f.result()
                    except Exception as exc:
                        crashes += 1
                        record_failure(primitive_name, exc)

            check_invariants(f"{primitive_name}:repeat{repeat}")

    inv_violations = len(_failures) - failures_before - crashes
    return {
        "primitive": primitive_name,
        "repeats": repeats,
        "crashes": crashes,
        "invariant_violations": inv_violations,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the full Pass B matrix and print a result table."""
    from pyfakefs.fake_filesystem_unittest import Patcher

    interpreter = (
        f"Python {sys.version_info.major}.{sys.version_info.minor}"
        f" [{sys.implementation.name}]"
    )
    gil_status = "GIL enabled"
    if hasattr(sys, "_is_gil_enabled"):
        gil_status = "GIL enabled" if sys._is_gil_enabled() else "GIL DISABLED"
    print(f"\nPass B — pyfakefs thread-safety stress")
    print(f"Interpreter: {interpreter} — {gil_status}")
    print(f"{'Primitive':<25} {'Repeats':>8} {'Crashes':>8} {'Inv.viol':>10}")
    print("-" * 55)

    primitives: list[tuple[str, Callable[[Any, int], None]]] = [
        ("mkdir_burst", mkdir_burst),
        ("open_burst", open_burst),
        ("stat_burst", stat_burst),
        ("write_burst", write_burst),
        ("link_burst", link_burst),
        ("walk_while_mutate", walk_while_mutate),
    ]

    results: list[dict[str, Any]] = []
    for name, prim in primitives:
        result = run_combo(name, prim, Patcher)
        results.append(result)
        marker = " *** FAIL ***" if (result["crashes"] or result["invariant_violations"]) else ""
        print(
            f"{result['primitive']:<25}"
            f" {result['repeats']:>8}"
            f" {result['crashes']:>8}"
            f" {result['invariant_violations']:>10}"
            f"{marker}"
        )

    print()
    total_crashes = sum(r["crashes"] for r in results)
    total_inv = sum(r["invariant_violations"] for r in results)
    print(f"Total crashes: {total_crashes}  Total invariant violations: {total_inv}")

    if _failures:
        print(f"\n--- First failure traceback ---")
        prim, exc_type, tb = _failures[0]
        print(f"Primitive: {prim}  Exception: {exc_type.__name__}")
        print(tb)

    if total_crashes or total_inv:
        sys.exit(1)


if __name__ == "__main__":
    main()
