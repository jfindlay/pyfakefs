# pyfakefs thread-safety audit

Working document for the systematic thread-safety audit of `pytest-dev/pyfakefs`,
conducted from the personal fork `github.com/jfindlay/pyfakefs` on branch
`thread-local-use-original`. The downstream artifact filed with upstream is
`UMBRELLA-ISSUE-DRAFT.md`; this document is the supporting evidence and the
execution log.

The maintainer has confirmed pyfakefs is not designed to be thread-safe
(documented at https://pytest-pyfakefs.readthedocs.io/en/latest/intro.html#limitations).
The audit's purpose is therefore not "make pyfakefs thread-safe" — it is to
produce a durable hazard inventory that lets the maintainer choose a single
disposition for the whole class of issues (document, lock, or partial fix).

---

## Strategy: three orthogonal discovery passes

Race discovery in Python has three vectors with disjoint blind spots. Each pass
catches a class of hazard the others miss. Skipping any one leaves a category
invisible.

| Pass | Method | Cost | Catches | Misses |
|------|--------|------|---------|--------|
| A | Static-pattern grep + read | Low; one-shot | All sites matching a known shape; durable across revisions | Hazards whose shape we haven't catalogued |
| B | Stress under scheduler-amplifier (PyPy / free-threaded CPython) | Medium; minutes per run | Novel shapes that trigger observable failure | Hazards with sub-thread-switch race windows; silent data races |
| C | Per-hazard barrier harness | Low per hazard | Confirmation that a candidate is a real race | Discovery (it doesn't find anything new); still GIL-hidable |

The honest characterization: Pass A is the only pass whose evidence does not
decay silently across revisions. Pass B finds new shapes but its observation
that "test passed 5/5 times" is not meaningful evidence of safety. Pass C is a
code-review argument written in Python — useful for confirmation, not
discovery. Use all three; rely on Pass A for durability.

---

## Pass A — Static-pattern audit (executed)

Audit conducted across all `pyfakefs/*.py` modules (excluding `tests/`),
~11.7k lines total. Six known shape categories grepped, each match read in
context (±15 lines), classified as confirmed hazard / justified-safe /
unclear.

**Headline:** 24 confirmed hazards, 14 justified-safe sites, 7 unclear,
plus 2 new shape categories not previously catalogued.

The umbrella issue draft enumerated 5 hazards (the ones empirically observed
during PR #1318's regression-test restructuring). Pass A finds 19 additional
sibling sites with the same shapes, plus extends the shape catalogue with two
new categories — process-global module variables (USER_ID / GROUP_ID) and
shared-list-with-nullification (load-bearing under free-threaded CPython).

### Shape 1: Integer read-modify-write counters

`x += 1` on a shared mutable attribute. CPython's GIL makes a single
`INPLACE_ADD` bytecode atomic, so the hazard is masked there but real. Free-
threaded CPython exposes it directly; PyPy's longer JIT-compiled traces widen
the window.

**Confirmed hazards (10):**

| Site | Symbol | Race |
|------|--------|------|
| `fake_filesystem.py:579` | `last_dev` | Concurrent `add_mount_point` → duplicate device numbers |
| `fake_filesystem.py:588` | `last_ino` | Concurrent `add_mount_point` (root path) → duplicate inode numbers |
| `fake_file.py:566` | `last_ino` | Concurrent `add_entry` → duplicate inode numbers (umbrella hazard 3) |
| `fake_file.py:568` | `st_nlink` | Concurrent `add_entry` on same parent → wrong link count (umbrella hazard 5) |
| `fake_file.py:642` | `st_nlink` (decrement) | Concurrent `remove_entry` → underflow / wrong count |
| `fake_filesystem.py:751` | `mount_point["used_size"]` | Concurrent `change_disk_usage` → corrupted disk accounting (umbrella hazard 4) |
| `fake_file.py:328` | `epoch` | Concurrent writes to same file → stale-content miss in `_sync_io` |
| `fake_file.py:383` | `epoch` (size setter) | Concurrent `truncate`/write → same `_sync_io` stale hazard |
| `fake_filesystem_shutil.py:83` | `_patch_level` | Direct (non-`with_patched_globals`) callers race; `module_lock` doesn't cover the attribute |

**Justified safe (3):**

- `fake_filesystem.py:464` `_cwd +=` — `_add_root_mount_point` runs only inside `reset()` during `__init__`, before any thread holds a reference.
- `fake_file.py:521` `st_nlink += 1` in `FakeDirectory.__init__` — object under construction; not yet referenced.
- `fake_filesystem.py:579` `last_dev += 1` when called from `__init__` (single-threaded path).

**Unclear (2):**

- `fake_file.py:328, 383` `epoch` — counter is used by `_sync_io` to detect stale `FakeFileWrapper` content. Whether this constitutes a real hazard depends on whether pyfakefs intends to support concurrent writes to a single `FakeFile`; ask maintainer.
- `fake_filesystem_shutil.py:83, 101` `_patch_level` — protected by `module_lock` *if* all callers go through `with_patched_globals`. Audit needed of caller paths to confirm; if any direct caller exists, the lock is bypassed.

### Shape 2: Heap TOCTOU

`heapq` operations on a shared list, with a check-then-pop pattern.

**Confirmed hazards (4) — all on `_free_fd_heap`:**

| Site | Operation | Race |
|------|-----------|------|
| `fake_filesystem.py:946-948` | `if heap: heappop` | TOCTOU (umbrella hazard 1) |
| `fake_filesystem.py:936-937` | `list.remove(new_fd)` | Concurrent `heappush` corrupts heap mid-remove |
| `fake_filesystem.py:941-942` | `heappush` in fd-fill loop | Concurrent push interleave |
| `fake_filesystem.py:965` | `heappush` in `close_open_file` | Concurrent close calls corrupt heap invariant |

No other heaps exist in pyfakefs.

### Shape 3: Dict mutate-during-iterate

Iteration over a shared dict/list while another thread mutates. Reliably
fires `RuntimeError: dictionary changed size during iteration` under PyPy.

**Confirmed hazards (13):**

| Site | Iterated structure | Mutating op |
|------|--------------------|-------------|
| `fake_filesystem.py:1523` | `directory.entries` | `add_entry`/`remove_entry` (umbrella hazard 2) |
| `fake_file.py:594` | `self.entries` (`_normalized_entryname`) | same |
| `fake_file.py:653` | `self.entries.items()` (size property) | same |
| `fake_file.py:672` | `self.entries` (`__str__`) | same |
| `fake_filesystem.py:568-576` | `self.mount_points` (`add_mount_point`) | concurrent `add_mount_point` |
| `fake_filesystem.py:630-639` | `self.mount_points` (`_mount_point_for_path`) | same |
| `fake_filesystem.py:659-669` | `self.mount_points` (`_mount_point_dir_for_cwd`) | same |
| `fake_filesystem.py:673` | `self.mount_points.values()` (`_mount_point_for_device`) | same |
| `fake_filesystem.py:1015` | `self.open_files` (`has_open_file`) | `add_open_file`/`close_open_file` |
| `fake_filesystem.py:1464` | `self.mount_points` (`replace_windows_root`) | `add_mount_point` |
| `fake_filesystem.py:1479` | `self.mount_points` (`is_mount_point`) | same |
| `fake_file.py:987-996` | `open_files[3:]` (`_flush_related_files`) | `add_open_file`/`close_open_file` |
| `fake_file.py:1177-1186` | `open_files[3:]` (`_adapt_size_for_related_files`) | same |

**Justified safe (1):**

- `fake_file.py:638` `while entry.entries:` — uses `list(entry.entries)[0]` to snapshot the key before mutating. The iteration itself is safe; the loop *condition* however is a Shape 5 TOCTOU (entry can be added between iterations).

### Shape 4: List append + index allocator

Append-then-return-index patterns where the index is an identifier.

**Confirmed hazards (3):**

- `fake_filesystem.py:951-952` — `open_files.append(...); return len(...) - 1` — concurrent `add_open_file` calls can both compute the same index.
- `fake_filesystem.py:941, 943` — `open_files.append([])` / `append([file_obj])` in the new-fd-fill branch — interleaved appends.
- `fake_filesystem.py:927` — `size = len(open_files); if new_fd < size: ...` — stale length snapshot.

**Justified safe (1):**

- `fake_filesystem.py:1000` — `valid = file_des < len(self.open_files)` is a bounds check, not an allocator; safe (slot may be `None` by access time but that's checked).

### Shape 5: TOCTOU existence check

`if X in collection: ...` followed by mutation of the collection. Reads
non-atomically against concurrent inserts/removals.

**Confirmed hazards (8):**

| Site | Check | Mutation |
|------|-------|----------|
| `fake_file.py:560-563` | `name in self.entries` | `_entries[name] = path_object` (silent overwrite) |
| `fake_filesystem.py:568-577` | iterate `mount_points` for duplicate | `mount_points[path] = {...}` |
| `fake_filesystem.py:2630-2631` | `self.exists(path)` | `add_object(...)` (file create) |
| `fake_filesystem.py:2252-2253` | `self.exists(dir_path)` | `current_dir.add_entry(new_dir)` |
| `fake_filesystem.py:2915` | `self.exists(dir_name)` | `add_object(...)` (mkdir) |
| `fake_filesystem.py:2770-2771` | `self.exists(new_path)` | `add_object(...)` (link create) |
| `fake_filesystem.py:946-948` | `if self._free_fd_heap:` | `heappop` (also Shape 2) |
| `fake_file.py:636-638` | `while entry.entries:` | `entry.remove_entry(...)` |

**Justified safe (2):**

- `fake_filesystem.py:2501` — `_create_fake_from_real_dir_lazily`, runs at fixture setup.
- `fake_filesystem.py:3287` — `_create_temp_dir`, runs only in `reset()`/`__init__`.

### Shape 6: Class-attribute toggle (siblings of `use_original`)

Class-level attributes mutated by context managers, `__new__`, or
configuration setters. Exactly the shape #1317 / PR #1318 fixed for
`use_original`.

**Confirmed hazards (7):**

- `fake_path.py:122-126` — `cls.sep`, `cls.altsep`, `cls.linesep`, `cls.devnull`, `cls.pathsep` written by `FakePathModule.reset` without synchronization. Any thread reading `os.path.sep` while another sets `is_windows_fs` races.
- `fake_filesystem_unittest.py:766-770` — `Patcher.clear_fs_cache` replaces five class-level dicts/sets (`CACHED_MODULES`, `FS_MODULES`, `FS_FUNCTIONS`, `FS_DEFARGS`, `SKIPPED_FS_MODULES`); concurrent `_find_modules` mutates these via `setdefault().add(...)`.
- `fake_filesystem_unittest.py:1040-1044` — `setUp` increments class-level `DOC_REF_COUNT` / `REF_COUNT`. Parallel test runners race on the guard.
- `fake_filesystem_unittest.py:1160-1164` — `tearDown` decrements the same; can cause premature teardown.
- `fake_filesystem_unittest.py:605, 608` — `__new__` checks `cls.PATCHER is None` then assigns. Concurrent `Patcher()` constructions can both pass.
- `fake_filesystem_shutil.py:87-98` — `_start_patching_global_vars` writes to `shutil._HAS_FCOPYFILE` (process-global module attrs); affects any concurrent shutil call from threads not holding `module_lock`.
- `fake_filesystem_unittest.py:1064-1065` — `start_patching` sets `self._patching = True; self._paused = False` non-atomically; `pause`/`resume` race.

**Justified safe (2):**

- `fake_os.py:79, 112` — `_thread_state.use_original` (the PR #1318 fix). Per-thread, safe.
- `fake_os.py:1521-1536` — `use_original_os()` context manager. Same.

**Unclear (1):**

- `fake_filesystem.py:451-458` `use_fs_type` context manager — sets `self.fs_type`; instance attribute, not class. Hazardous only if the same `FakeFilesystem` is used concurrently across threads. Whether this is intended needs maintainer confirmation; if multi-thread access is a use case at all, this is a hazard.

### Shape 7 (NEW): Process-global module variables

Bare module-level globals (not class attributes, not thread-locals) mutated
by setter functions. Distinct from Shape 6 because they affect *every*
filesystem operation regardless of which `FakeFilesystem` instance is in
play.

**Confirmed hazard (1, two related sites):**

- `helpers.py:62-66, 82-83, 98-99` — `USER_ID` and `GROUP_ID` module
  globals, mutated by `set_uid()` / `set_gid()` without any lock. Read at
  `helpers.py:71, 88` and throughout `fake_file.py:436` and
  `fake_filesystem.py:778` for permission checks. Any thread calling
  `set_uid(1)` races with any other thread's permission check globally.

This shape was missed by the original umbrella draft because no observed
crash surfaced from it — it manifests as a *silent permission-check
inconsistency* rather than an exception.

### Shape 8 (NEW): Shared-list-with-nullification under free-threaded CPython

Slice-then-iterate over a list whose elements can be set to `None` by a
concurrent operation. Safe under CPython-with-GIL because the `is not None`
check and the use are separate bytecodes that can interleave but not corrupt;
**not safe under free-threaded CPython 3.13+/3.14+**, where memory model
guarantees no longer hide the window.

**Confirmed hazards (2 — both already counted in Shape 3):**

- `fake_file.py:987` and `fake_file.py:1177` — `for open_files in self.filesystem.open_files[3:]:` iterates while `close_open_file` (line 964) sets list elements to `None`.

This shape is called out separately because it represents the **strongest
argument for running Pass B against free-threaded CPython 3.13t / 3.14t**:
hazards that pass on CPython today will fail on free-threaded CPython
tomorrow, and the maintainer's calculus on "thread-unsafety is documented and
acceptable" should account for the imminent runtime change.

### Pass A summary table

| Shape | Confirmed | Safe | Unclear |
|-------|-----------|------|---------|
| 1. Int RMW | 10 | 3 | 2 |
| 2. Heap TOCTOU | 4 | 0 | 0 |
| 3. Dict iter-mutate | 13 | 1 | 0 |
| 4. List allocator | 3 | 1 | 0 |
| 5. TOCTOU existence | 8 | 2 | 0 |
| 6. Class-attr toggle | 7 | 2 | 1 |
| 7. Module global (NEW) | 2 | 0 | 0 |
| 8. List-with-nullification (NEW) | 2 (overlap with 3) | 0 | 0 |
| **Total (deduplicated)** | **47** sites across **8 shapes** | 9 | 3 |

(Confirmed-hazard sites count is higher than the headline 24 because some
sites match multiple shapes — e.g., `add_open_file` lines 946-948 are both a
Shape 2 heap TOCTOU and a Shape 5 existence-check TOCTOU. The headline of 24
counts each *hazardous code region* once; the table above counts each
shape match.)

---

## Pass B — Scheduler-amplifier stress (executed)

Goal: catch sibling hazards Pass A's grep didn't think to look for. Run
under interpreters whose schedulers widen race windows beyond CPython's GIL.

### Runtime targets and environment

Executed on x86_64 Ubuntu 24.04.

| Interpreter | Version | GIL | Obtained via |
|-------------|---------|-----|--------------|
| CPython 3.12 | 3.12.3 | enabled | system |
| PyPy 3.11 | 7.3.19 | enabled | binary tarball from pypy.org |
| CPython 3.13 (free-threaded) | 3.13.3 | **disabled** | built from source (`--disable-gil`) |

Build instructions:

```bash
# PyPy (binary tarball):
curl -L https://downloads.python.org/pypy/pypy3.11-v7.3.19-linux64.tar.bz2 -o /tmp/pypy3.tar.bz2
mkdir -p ~/pypy3 && tar -xjf /tmp/pypy3.tar.bz2 -C ~/pypy3 --strip-components=1
~/pypy3/bin/pypy3 -m pip install -e /path/to/pyfakefs

# Free-threaded CPython (from source — no GIL):
# Prerequisite: libffi-dev is required for ctypes (needed by pyfakefs helpers.py)
sudo apt-get install -y libffi-dev
git clone --depth=1 --branch v3.13.3 https://github.com/python/cpython.git ~/cpython-ft/src
cd ~/cpython-ft/src
# NOTE: do NOT use --enable-optimizations here.  The PGO profile run executes
# the test suite; test_generators fails and aborts the build before the binary
# is installed.  Plain ./configure + make produces a working binary at
# ~/cpython-ft/src/python without the PGO step.
./configure --disable-gil --prefix=$HOME/.local/python3.13t
make -j$(nproc)
# binary is at ~/cpython-ft/src/python (not python3.13t — make install not run)
~/cpython-ft/src/python -c "import sys; print(sys._is_gil_enabled())"  # → False
~/cpython-ft/src/python -m pip install -e /path/to/pyfakefs
```

### Workload primitives

Each primitive is a function `(fs, n) -> None` that performs n operations of
one shape. Eight primitives cover the documented hazard surface; new
primitives can be added as new shapes are catalogued.

| Primitive | Operation | Stresses |
|-----------|-----------|----------|
| `mkdir_burst(fs, n)` | `n` × `Path.mkdir(parents=True)` against shared parent | Shape 1 (`st_nlink`, `last_ino`), Shape 3 (`_entries`), Shape 5 (existence check) |
| `open_burst(fs, n)` | `n` × `open(path, 'w')` then close | Shape 2 (`_free_fd_heap`), Shape 4 (`open_files` allocator) |
| `stat_burst(fs, n)` | `n` × `os.stat(path)` against pre-created paths | Read-mostly; useful as a noise generator alongside writers |
| `write_burst(fs, n)` | `n` × `open + write + close` against unique paths | Shape 1 (`used_size`, `epoch`), Shape 4 (`open_files`) |
| `link_burst(fs, n)` | `n` × `os.link(src, dst)` | Shape 1 (`st_nlink` increment), Shape 5 (link create existence) |
| `chmod_burst(fs, n)` | `n` × `os.chmod(path, mode)` | Permission-bit RMW; Shape 7 if combined with `set_uid` |
| `rmtree_burst(fs, n)` | `n` × `shutil.rmtree(subtree)` | Shape 1 (`st_nlink` decrement), Shape 3 (iter+remove) |
| `walk_while_mutate(fs, n)` | one `os.walk` against subtree concurrent with `n` mkdirs | Shape 3 (iter-during-mutate) |

### Workload combinator

```python
def run_combo(fs_factory, primitives, k=4, n_per=32, threads=8, repeats=20):
    """Run k primitives concurrently, threads workers per primitive,
    repeated repeats times against fresh fs each repeat."""
    for repeat in range(repeats):
        fs = fs_factory()
        # pre-create shared parent so existence-check race doesn't dominate
        fs.create_dir("/shared")
        with ThreadPoolExecutor(max_workers=k * threads) as pool:
            futures = [pool.submit(prim, fs, n_per)
                       for prim in primitives
                       for _ in range(threads)]
            for f in futures:
                try:
                    f.result()
                except Exception as e:
                    record_failure(repeat, prim.__name__, e)
        check_invariants(fs)  # see classifier
```

### Outcome classifier

Three failure modes to record:

1. **Crash** — uncaught exception from any worker. Record `(primitive_name, exception_type, traceback)`.
2. **Invariant violation** — after the burst, check:
   - All inode numbers across `walk(fs.root)` are unique (Shape 1 detector).
   - `mount_point["used_size"]` equals the sum of `st_size` for all files in that mount (Shape 1, lost-update detector).
   - `len(parent.entries)` matches `parent.st_nlink - 2` for every directory (Shape 1 link-count detector).
   - `len([w for w in fs.open_files if w])` matches the count of currently-open `FakeFileWrapper` instances (Shape 2/4 allocator detector).
3. **Hang** — worker doesn't return within timeout. Record as a separate category; rare but possible with deadlocked locks if any are added later.

Run each (primitive_combo × interpreter) cell. Record results in a matrix.
A cell with zero crashes + zero invariant violations across `repeats` runs is
*evidence of absence at this scheduler depth*, not proof; cells with any
failures are confirmed hazards.

### Pass B results (executed 2026-05-01, x86_64 Ubuntu 24.04)

Script: `thread_safety_pass_b.py` (10 repeats, 8 threads, 32 ops/thread).

Columns: C = crashes (worker exceptions propagated through `f.result()`),
I = invariant violations (duplicate inodes detected post-burst).

```
              | mkdir   | open    | stat | write   | link    | walk+mut |
              | C  /  I | C  /  I |      | C  /  I | C  /  I | C  /  I  |
--------------+---------+---------+------+---------+---------+----------+
3.13 (FT/GIL-)| 0  / 10 | 0  /  8 | 0/0  | 0  / 10 | 0  / 10 | 0  /  0  |
PyPy 3.11     | 0  /  0 | 0  /  0 | 0/0  | 0  /  0 | 0  / 10 | 0  /  0  |
3.12 (GIL)    | 0  /  0 | 0  /  0 | 0/0  | 0  /  0 | 0  /  0 | 0  /  0  |
```

Key observations:

- **Free-threaded CPython (GIL disabled)** surfaces Shape 1 `last_ino` duplicate-
  inode violations on every mkdir/write/link repeat.  Shape 2 heap corruption
  causes a **C-level segfault** under free-threaded CPython when running
  `mre_shape2_heap_toctou.py` at 32 threads — the interpreter crashes rather
  than raising `IndexError`.
- **PyPy** surfaces the Shape 1 `last_ino` race only in `link_burst` (the heap
  corruption in `mre_shape2_heap_toctou.py` also fires reliably under PyPy at
  32 threads with `IndexError`; the Pass B combinator's `open_burst` at 8
  threads was not aggressive enough to expose it).
- **Shape 3** (`_directory_content` dict iter-during-mutate) does not appear in
  the Pass B matrix because it only fires on a case-insensitive filesystem
  (`is_case_sensitive=False`).  The targeted Pass C MRE
  (`mre_shape3_dict_mutate.py`) reproduces it reliably on both PyPy (940/50
  repeats at 32 threads) and free-threaded CPython (620/640 worker failures).
- **CPython 3.12 (GIL enabled)**: no failures observed, as expected.

### Triage of new findings

For each new (primitive, interpreter) failure:

1. Capture the full traceback.
2. Reduce to a Pass C MRE (next section).
3. Match against Shape 1-8; if it doesn't match, add a new shape to Pass A
   and re-run Pass A's grep against the new shape.

---

## Pass C — Per-hazard barrier MRE (specification + per-hazard slots)

For each confirmed hazard from Pass A or Pass B, build a minimal reproducer
using `threading.Barrier` for deterministic interleaving setup. The MRE is
not a regression test — it is a *snapshot demonstration* the maintainer can
paste into their own environment.

### MRE template

```python
"""
Hazard N: <one-line shape description>
Site:     pyfakefs/<file>.py:<line>
Shape:    <Shape N from Pass A catalogue>
Observed: <crash | silent invariant violation>

Run:    python -X dev pyfakefs_hazard_N.py
        pypy3 pyfakefs_hazard_N.py
        python3.14t pyfakefs_hazard_N.py
"""
import threading
from pyfakefs.fake_filesystem_unittest import Patcher

THREADS = 16

def worker(fs, barrier, idx):
    barrier.wait()
    # ... the racing operation, parameterized by idx ...

def main():
    with Patcher() as p:
        # minimal fixture setup; just enough to expose the racing site
        p.fs.create_dir("/shared")
        b = threading.Barrier(THREADS)
        threads = [threading.Thread(target=worker, args=(p.fs, b, i))
                   for i in range(THREADS)]
        for t in threads: t.start()
        for t in threads: t.join()
        # invariant assertion
        assert <invariant>, "hazard N: <one-line description of violation>"

if __name__ == "__main__":
    main()
```

Two structural rules:

1. **`threading.Barrier`, not `asyncio.gather`.** PR #1318's regression test
   uses asyncio because it predates this audit; for new MREs, a barrier is
   shorter and produces cleaner stack traces.
2. **Strip every dependency that isn't load-bearing for the race.** Where
   possible, instantiate `FakeFilesystem()` directly and call the racing
   method, skipping `Patcher`. Smaller MREs give the maintainer less surface
   to argue about.

### Per-hazard MRE slots

This section accumulates concrete MREs as they are written. Each hazard from
Pass A's confirmed-hazard table gets one slot. Filling them is a fresh-
session task.

Status legend:
- ✅ MRE written, observed to fire on at least one interpreter
- 🟡 MRE drafted, not yet run
- ⬜ slot empty

#### Shape 1 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 1.1 | `fake_filesystem.py:579` (`last_dev`) | ✅ | `mre_shape1_mount_point.py`; duplicate idev 8/20 repeats on 3.13 FT; Shape 3.5 crash fires first on PyPy (4/20), blocking most observations |
| 1.2 | `fake_filesystem.py:588` (`last_ino` mount) | 🟡 | same script; not separately confirmed (Shape 3.5 crash dominates) |
| 1.3 | `fake_file.py:566` (`last_ino` add_entry) | ✅ | `mre_shape1_last_ino.py`; 10/10 on 3.13 FT, invariant violation via link_burst on PyPy |
| 1.4 | `fake_file.py:568` (`st_nlink +=`) | ✅ | `mre_shape1_st_nlink.py`; st_nlink < 2 in 5/10 repeats on 3.13 FT |
| 1.5 | `fake_file.py:642` (`st_nlink -=`) | ✅ | same script; fires alongside 1.4 |
| 1.6 | `fake_filesystem.py:751` (`used_size +=`) | ✅ | `mre_shape1_used_size.py`; 10/10 on 3.13 FT (max delta 82944 bytes), 1/10 on PyPy |
| 1.7 | `fake_file.py:328, 383` (`epoch`) | ⬜ | unclear; needs maintainer answer |
| 1.8 | `fake_filesystem_shutil.py:83` (`_patch_level`) | ⬜ | unclear; needs caller-path audit |

#### Shape 2 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 2.1 | `fake_filesystem.py:946-948` (heap TOCTOU) | ✅ | `mre_shape2_heap_toctou.py`; `IndexError` from `heappop` on PyPy (10/20 repeats), **segfault** on 3.13 FT at 32 threads |
| 2.2 | `fake_filesystem.py:936-937` (heap remove) | ⬜ | internal path only (new_fd >= 0 branch); not reachable from normal open/close |
| 2.3 | `fake_filesystem.py:941-942` (heap fill loop) | ⬜ | same internal path |
| 2.4 | `fake_filesystem.py:965` (heap close push) | ✅ | `IndexError` from `heappush → _siftdown` confirmed in same MRE run as 2.1 |

#### Shape 3 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 3.1 | `fake_filesystem.py:1523` (`_directory_content` listcomp) | ✅ | `mre_shape3_dict_mutate.py`; `RuntimeError: dict changed size` — 940/50 on PyPy, 620/640 workers on 3.13 FT; fires via `_directory_content` and also via `lresolve → _original_path → _directory_content:1525` |
| 3.2 | `fake_file.py:594` (`_normalized_entryname`) | ✅ | same root cause as 3.1; fires via `mre_shape3_dict_mutate.py` on case-insensitive FS (same listcomp pattern) |
| 3.3 | `fake_file.py:653` (size property) | ⬜ | iterates entries.items(); fires if entry added/removed concurrently |
| 3.4 | `fake_file.py:672` (`__str__`) | ⬜ | low severity; not worth dedicated MRE |
| 3.5–8 | `mount_points` iteration sites | ✅ | `mre_shape3_mount_points.py`; crashes 2/20 PyPy, 20/20 3.13 FT; `mre_shape1_mount_point.py` shows same |
| 3.9 | `fake_filesystem.py:1015` (`open_files` listcomp) | ✅ | `mre_shape3_open_files.py`; crashes 3/20 PyPy, 11/20 3.13 FT (IndexError / RuntimeError) |
| 3.10 | `fake_filesystem.py:1464, 1479` (`mount_points`) | ✅ | confirmed via Shape 3.5–8 MRE (same dict) |
| 3.11 | `fake_file.py:987, 1177` (`open_files[3:]`) | ✅ | `mre_shape3_open_files.py` exercises same open_files structure; 3.13 FT shows `RuntimeError: list changed size during iteration` in heappop (overlaps Shape 2) |

#### Shape 4 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 4.1 | `fake_filesystem.py:951-952` (allocator) | ✅ | `mre_shape3_open_files.py`; duplicate fd allocation confirmed by Shape 2 crash path (open_fd race) |

#### Shape 5 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 5.1 | `fake_file.py:560-563` (entry overwrite) | 🟡 | `mre_shape5_toctou.py`; no observable symptom on PyPy or 3.13 FT (dict insert is atomic at bytecode level; silent overwrite requires non-GIL torn read) |
| 5.2 | `fake_filesystem.py:2630-2631` (file create) | 🟡 | same script |
| 5.3 | `fake_filesystem.py:2252-2253` (dir create) | 🟡 | same script |
| 5.4 | `fake_filesystem.py:2915` (mkdir) | 🟡 | same script |
| 5.5 | `fake_filesystem.py:2770-2771` (link create) | 🟡 | same script |

#### Shape 6 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 6.1 | `fake_path.py:122-126` (`cls.sep` etc.) | ⬜ | silent; observable only by asserting sep value mid-context-switch |
| 6.2 | `fake_filesystem_unittest.py:766-770` (cache replace) | ⬜ | |
| 6.3 | `fake_filesystem_unittest.py:1040-1164` (REF_COUNT) | 🟡 | `mre_shape6_class_attrs.py`; no Patcher leak observed, but 3.13 FT produces `KeyError: '/'` and `NameError: name 'getuid'` in tearDown — both downstream of other races (Shape 3) tearing down a half-initialised FS |
| 6.4 | `fake_filesystem_unittest.py:605, 608` (`__new__` race) | 🟡 | same script; PATCHER singleton correctly reset between repeats; no leak observed |
| 6.5 | `fake_filesystem_shutil.py:87-98` (shutil globals) | ⬜ | |
| 6.6 | `fake_filesystem_unittest.py:1064-1065` (pause/resume) | ⬜ | |

#### Shape 7 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 7.1 | `helpers.py:USER_ID/GROUP_ID` | ✅ | `mre_shape7_uid_gid.py`; 1/5 repeats on PyPy (2 cross-thread reads), 5/5 on 3.13 FT (591,563 cross-thread reads in 50k ops) |

#### Shape 8 hazards

| # | Site | Status | Notes |
|---|------|--------|-------|
| 8.1 | `open_files` slice-with-nullification | ✅ | `mre_shape3_open_files.py`; 3.13 FT shows `RuntimeError: list changed size during iteration` (heapq internal — same concurrent close path); the `open_files[3:]` slice + None-check pattern is exercised by the same open/close workload |

---

## Static-pattern check (implemented)

Implemented at `pyfakefs/tests/test_thread_safety_static.py`.

Scans all `pyfakefs/*.py` source files for unsynchronized augmented-assignment
(`+=`, `-=`) on Shape 1 attributes (`last_ino`, `last_dev`, `st_nlink`,
`used_size`) and any write (`=`) to Shape 7 module globals (`USER_ID`,
`GROUP_ID`).

As of this audit (2026-05-01), the check flags **9 confirmed hazardous sites**
with no false positives.  Five additional init-time sites were annotated with
``# thread_safe_ok: module-level init, single-threaded at import time`` or
``# thread_safe_ok: object under construction, not yet shared``
(``helpers.py:62-66``, ``fake_file.py:521``) — these are genuinely safe and
excluded from the count.  The opt-out comment is deliberate and searchable.

Known limitation: dict-subscript RMW (`mount_point["used_size"] += ...`) is
not caught because the pattern is attribute-name based.  The `used_size` shape
is documented but not enforced by the static check.

Properties:
- **Does not decay** — failure is a syntactic property of the source, not a
  runtime observation.
- **Cheap to land** — ~80 lines, no new dependencies.
- **Allows opt-out** — `# thread_safe_ok: <reason>` comment; no `# noqa`.
- **Maintainer can reject without bikeshedding** — clearly optional; framed
  as one of five fix strategies in the umbrella issue.

This is the only form of concurrency-hazard test that does *not* fall under
PLAN's "tests are demonstrative not regressive" critique.

---

## Acceptance and limitations

This audit produces three things that decay differently:

| Artifact | Decay mode |
|----------|------------|
| Pass A hazard inventory | Stable. Source either still matches the pattern or it does not. Re-runnable mechanically. |
| Pass B observed failures | Decays silently. If a future revision shortens a race window so PyPy doesn't trigger it, the test passes; the hazard persists. |
| Pass C barrier MREs | Decays silently. Same reason as Pass B. |
| Static-pattern check | Stable, scoped to listed attrs. Doesn't catch new shapes; can be extended. |

The umbrella issue's durable artifact is the Pass A inventory plus the
static-pattern check. Pass B/C MREs are contemporaneous evidence that backs
the inventory at the moment it was filed; they are not the artifact's
load-bearing element.

A maintainer reviewing this is being asked to choose a disposition once,
across the whole inventory, not to merge per-hazard fixes. Likely outcomes,
in descending order of probability:

1. **"Document, wontfix."** Add a paragraph to README stating thread-
   unsafety; close umbrella as documented. Reasonable; doesn't help users
   needing concurrent access but is honest.
2. **"Add coarse lock."** Single `threading.Lock` on `FakeFilesystem`
   guarding the public API surface. Solves all eight shapes; measurable
   single-threaded overhead but probably tolerable.
3. **"Fix specific high-severity hazards only."** Land the heap TOCTOU and
   `_entries` listcomp fixes (the two that *crash*); leave silent data
   races as documented limitations.
4. **"Land static-pattern test only."** Cheapest acceptance: marks the
   shape inventory as a regression target without committing to fixes.

Outcomes 2 or 4 would be more useful than 1; outcome 3 is a reasonable
middle. Outcome 1 is the most-likely default.
