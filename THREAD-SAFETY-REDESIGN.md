# pyfakefs thread safety: a first-principles redesign proposal

**Status:** Discussion draft for the maintainer and the umbrella issue thread.
**Branch:** `fix-thread-races`.
**Companion documents:** `THREAD-SAFETY-AUDIT.md` (Pass-A static audit, eight catalogued hazard
shapes), `PLAN.md` (post-retrofit cleanup), `CHANGES.md` (retrofit changelog entries).

---

## Executive summary

**The retrofit's premise — that pyfakefs can be made thread-safe by adding locks to the existing API
— is not workable, because pyfakefs is missing the abstraction layer that should own the locking.**
A real filesystem locks internally: userspace calls `open()`, the kernel acquires inode and dentry
locks scoped to the operation, and userspace gets atomic semantics without ever touching a lock.
pyfakefs's current architecture has no equivalent layer. The patched `os`/`pathlib`/`io` modules
(the syscall layer) call directly into the `FakeFilesystem` object graph; there is nothing between
them to host locking, so the retrofit put the locks on the public-method boundary instead. That
works for the call's body but fails at the call's *return*: `fs.get_object(p)` returns a live
mutable `FakeFile`, `directory.entries` returns the live `_entries` dict, `fs.root_dir` returns the
actual root directory. Once those references escape the lock boundary outward, no locking discipline
can keep them safe. Users who write `f = fs.get_object('/foo'); f.contents = b'new'` have
unwittingly escaped every lock in the library, and the API gives them no signal that this is wrong —
the retrofit's auto-applied `_with_lock` wrapper creates a *false floor* of safety that users
reasonably believe in and then fall through.

This is not a bug we can patch — it is the consequence of an absent architectural layer. The fix is
to introduce a **fake-VFS layer** that sits between the patched modules and the in-memory filesystem
state, owns its own locking, and exposes its results as values and handles rather than live
references. This is the same shape the real kernel has: a VFS above the inode/dentry/page-cache
layers, with locking internal to each and an opaque-fd interface to userspace. The fake doesn't need
the real kernel's per-inode-lock granularity — pyfakefs serves a few thousand operations per test,
not billions per second, so a coarse lock inside the fake-VFS layer is correct engineering for the
workload. What matters is that the locking lives *inside* the abstraction, not at the boundary where
users hold references.

The maintainer has stated that pyfakefs is not designed to be thread-safe, and the ReadTheDocs
limitations page reflects that. We agree with the stated position; this report is an argument that
*if* the library is to become thread-safe, the path is not "more locks" but "fewer escaped
references." The retrofit on `fix-thread-races` (commits `2c08ed1..143565d`) — coarse
`FakeFilesystem._lock`, `Patcher._class_lock`, per-thread `USER_ID`/`GROUP_ID`, per-instance
`FakePathModule` separator state — is a substantial improvement on the prior baseline, and we have
published it as a candidate for upstream merge. But it is the floor, not the ceiling.

A follow-up re-audit on the post-retrofit code identified **19 additional hazards** in seven
structural clusters that the retrofit does not close.  A subset of these can be patched with finer
locking (umask, cwd setter, the `_with_lock` blind spot for property setters, `_is_open` reads). The
remainder are not lock-shaped: they arise from architectural decisions — class-level singleton state
in `FakePath` and `FakeShutilModule`, process- global mutation of `tempfile.tempdir` and
`sys.meta_path`, the `Patcher.tearDown` lock-release window between `REF_COUNT` decrement and
`PATCHER = None`, user-supplied callbacks invoked while holding the filesystem lock, and the
API-shape problem just stated.

The redesign has one load-bearing change that subsumes the rest:

**Introduce a fake-VFS layer between the patched modules and the in-memory state, and let it own the
locking.** Concretely:

- The patched modules (`FakeOsModule`, `FakePathModule`, `FakePathlibModule`, `FakeIoModule`) become
  a thin syscall-translation layer that converts user calls into fake-VFS operations and converts
  fake-VFS results back into the values userspace expects (integers for fds, `os.stat_result` for
  stats, bytes for file contents).
- The fake-VFS layer owns the inode table, the dentry/path cache, the open-file table, and the
  page-cache-equivalent (file contents). It exposes operations (`vfs.open`, `vfs.read`, `vfs.write`,
  `vfs.lookup`, `vfs.create`, `vfs.unlink`) that take values in and return values out.  Locking is
  internal to this layer, not visible at the boundary.
- The `FakeFile` and `FakeDirectory` types stop being part of the public API. They become
  implementation details of the fake-VFS layer, comparable to `struct inode` and `struct dentry` in
  the kernel — never exposed to userspace.
- Public entry points return values and opaque handles: `stat_result`, `bytes`, integer file
  descriptors, `FakePath` objects (which are values), context-manager-scoped editors. The
  `_with_lock` auto-wrapping pattern goes away because the locking is no longer at the public-method
  boundary.

Everything else in this report follows from this. The four hazard clusters that require redesign (A,
B, C, D from §2) all dissolve when the fake-VFS layer is the locking domain. The §3 architectural
critiques (singleton, mutable shared graph, monkey-patching as integration model) each become
specific design decisions inside the new layer. The migration in §6 is the path from where we are to
this destination.

The phased migration preserves the public API for the documented patterns at every step. Code that
interacts with the filesystem through the patched modules — `os.open(path)`, `open(path).read()`,
`pathlib.Path(p).read_bytes()`, `fs.create_file(path)` — is unaffected, because that is the syscall
surface and it has the right shape already.  Code that reaches behind the syscall layer for live
`FakeFile` / `FakeDirectory` references — `fs.get_object(p).contents = b'new'`,
`directory.entries[name] = ...` — is the surface that becomes deprecated and ultimately removed.
That surface is undocumented in the user-facing guide and is used primarily by tests of pyfakefs
itself; downstream impact is bounded.

We treat **free-threaded CPython (PEP 703 / 3.13t)** as a first-class target. Several hazards in §3
are GIL-masked — they pass on stock CPython 3.10–3.13 and fail on `--disable-gil` builds. As CPython
moves toward GIL-less by default, those hazards become user-visible bugs. Designing for the GIL-less
invariant ("no torn reads of `int`, `str`, `list`, `dict`") forces the right architecture today.

This report is a discussion document, not a pull request. The redesign is substantial enough that we
want maintainer agreement on direction before investing in implementation. Concrete patches for the
lock-shaped hazards (§3 cluster B/E/G subsets) are easy to ship as follow-ups to the retrofit; the
architectural changes (§5) deserve their own design conversation.

---

## §1. What the retrofit fixed and what it didn't

The eight catalogued shapes in `THREAD-SAFETY-AUDIT.md` and the retrofit commits that addressed
them:

| Shape | Hazard | Retrofit commit | Disposition |
|-------|--------|-----------------|-------------|
| 1 | Integer `+=` on shared counters (`last_ino`, `last_dev`, `st_nlink`, `used_size`, `epoch`, `_patch_level`) | `253716c` | **Structurally fixed** by `_with_lock` auto-application; static check in `test_thread_safety_static.py` prevents regression |
| 2 | Heap reallocation TOCTOU on `_byte_contents` | `253716c` | **Structurally fixed** — all reads/writes of `_byte_contents` now hold `_lock` |
| 3 | Dict iteration during mutation (`_directory_content`, `mount_points`, `open_files`) | `253716c` | **Mostly fixed** under the lock; iterator escapes documented (Shape 5) but live views still returned (see §3 cluster E) |
| 4 | Iterator-return semantics in public APIs (`scandir`, `walk`) | `67acb83` (docs only) | **Documented**, not fixed; callers must hold `fs.lock()` for multi-step iteration |
| 5 | Live-reference TOCTOU on `entries` and similar properties | `67acb83` (docs only), `f51ead6` (`fs.lock()` public API) | **Documented**, not fixed; the live-mutable-view pattern remains (§3 cluster E re-encounters this) |
| 6 | Class-attribute toggle (`FakePathModule.sep` etc.) | `c0c8f9c` | **Structurally fixed** by demoting class attrs to per-instance properties with dynamic fallback to `self.filesystem.*` |
| 7 | Per-thread UID/GID (`USER_ID`, `GROUP_ID` module globals) | `c0c8f9c` | **Structurally fixed** by `threading.local`; documented per-thread semantics |
| 8 | Patcher singleton REF_COUNT and module-cache state | `7a953e6`, `143565d` | **Mostly fixed**; the `tearDown` release window (PLAN.md O1) remains structural |

**The fix register:** Shapes 1, 2, 6, 7 are structurally closed. Shapes 3 and 8 are closed against
the most-acute failure modes but leave residual hazards (§3 clusters A, E). Shapes 4 and 5 are
documented rather than fixed — callers must opt into safety via `fs.lock()`, which is a contract not
a guarantee.

The retrofit's pattern — a coarse `RLock` auto-applied to every public `FakeFilesystem` method by an
`inspect.getmembers` loop — is correct as far as it goes, but creates two new architectural concerns
(PLAN.md O2, O3) that this report develops in §3 and §4: the auto-wrapping pattern locks methods
that should not be locked (`@contextmanager` methods, pure getters), and it locks future methods
automatically without forcing the author to think about whether the lock is appropriate.

The retrofit does NOT address:

- **Lock ordering.** `Patcher._class_lock` and `FakeFilesystem._lock` can be acquired in either
  order across the public API. No formal lock hierarchy is documented; deadlock is currently avoided
  by accident, not by design (§3 cluster A).
- **Process-global state.** `tempfile.tempdir`, `sys.meta_path`, and the real `os.umask()`
  side-effect read in `FakeFilesystem.__init__` are unprotected (§3 cluster F).
- **Per-instance vs. per-class state for non-`FakePathModule` classes.** The Shape 6 fix established
  the per-instance pattern but did not extend it to `FakePath` (in `fake_pathlib.py`) or
  `FakeShutilModule` (§3 cluster C, D).
- **Callback re-entry.** User-supplied `side_effect` callbacks and lazy-load property bodies
  (`FakeDirectoryFromRealDirectory.entries`) run while the filesystem lock is held (§3 cluster B).

---

## §2. Hazard taxonomy: 19 additional findings beyond the eight shapes

The re-audit (executed by the `@explore` subagent against post-retrofit HEAD on `fix-thread-races`,
methodology in appendix A) identified 19 hazards in seven structural clusters. Confidence levels: 12
high, 6 medium, 1 speculation. Each hazard is reproducible at a specific file:line and has a
one-sentence structural cause.

We organise by structural cause rather than file location: hazards in different files that share a
root cause go in the same cluster, because the fix shape is shared.

### Cluster A: Lock-ordering and critical-section gaps in Patcher lifecycle

**Common cause:** `Patcher._class_lock` is released between phases of the patcher lifecycle (setUp,
tearDown, REF_COUNT update, module-cache mutation), creating windows where other threads observe
inconsistent singleton state.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| A.1 | `fake_filesystem_unittest.py:1174-1190` | `tearDown` releases `_class_lock` after `REF_COUNT -= 1`, runs `stop_patching()` and `reset_ids()` unlocked, then re-acquires to null `PATCHER`. A concurrent `__new__` between the two acquisitions returns a half-torn-down singleton. | redesign |
| A.2 | `fake_filesystem_unittest.py:1063-1073` | `setUp` runs `_find_modules()` and `_refresh()` outside `_class_lock`. Concurrent `setUp` from a second thread interleaves the two `_find_modules()` calls; `_refresh()` constructs a new `FakeFilesystem` whose assignment to `self.fs` races. | redesign |
| A.3 | `fake_filesystem_unittest.py:668-686` | `__init__` reads `REF_COUNT` and `DOC_REF_COUNT` without `_class_lock` to decide whether to skip re-initialisation. | locking |
| A.4 | `fake_filesystem_unittest.py:1078-1080` | `start_patching` writes `_patching = True; _paused = False` as two unlocked assignments; `pause()`/`resume()` read both without a lock. | locking |

**Why this is structural, not just bugs.** A.3 and A.4 can be patched with additional `_class_lock`
acquisitions. A.1 and A.2 cannot: they require either holding `_class_lock` across `stop_patching()`
(which itself mutates module-level state and would create a lock-order dependency with any
module-level lock that user code holds), or restructuring the lifecycle so that the unsafe window
does not exist.

The retrofit's choice to release `_class_lock` mid-`tearDown` was deliberate (the inline comment
cites avoiding deadlock during `smart_unset_all`). The right fix is not "hold the lock longer" but
"do not require the lock across the whole sequence" — which means the singleton's lifecycle must be
reorganised so that observable state transitions are atomic without requiring a held lock across
them.

### Cluster B: User-supplied callback invoked under lock

**Common cause:** library code invokes a callback (user-supplied or property-body code that calls
back into the public API) while holding `FakeFilesystem._lock`. Re-entrancy is safe for the same
`RLock`, but deadlock-prone if the callback acquires any *other* lock.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| B.1 | `fake_file.py:350-351` | `FakeFile.set_contents()` calls `self._side_effect(self)` (a user-supplied callable) while `_lock` is held transitively from `flush()` → `set_contents()`. | redesign |
| B.2 | `fake_file.py:748-764` | `FakeDirectoryFromRealDirectory.entries` lazy-loads by calling `add_real_file/add_real_directory/add_real_symlink` (lock-wrapped public methods) and the **real** `os.listdir/islink/isdir` while `_lock` is held. Real OS I/O latency stalls all other filesystem threads. | redesign |
| B.3 | `fake_file.py:748-749` | Check-then-set of `contents_read` in the same property is not atomic; two threads can both enter the load block and produce duplicate `add_real_*` calls that fail with `EEXIST` for every entry. | locking |

**Why B.1 and B.2 are redesign-shaped.** In B.1, the side-effect callback's contract is unspecified
— users write side-effect callbacks that touch their own application state, which may include their
own locks. There is no general fix that keeps the callback inside the lock; either the contract
becomes "side effects must not acquire locks" (fragile), or the callback is deferred until after
lock release (changes ordering semantics).

B.2 is worse: lazy-loading from the real filesystem inside the property body means a
`FakeDirectoryFromRealDirectory.entries` access can block on real-disk I/O while every other thread
waits on `_lock`. Tests that mount a slow filesystem (NFS, encrypted volumes) will stall under
contention.  The structural fix is to populate `entries` eagerly at construction time (no lazy
load), or to use a per-node lock for the lazy-load with a double-checked `contents_read` flag.

### Cluster C: `FakePath.filesystem` class-attribute singleton

**Common cause:** `pathlib.Path` faking uses a class-level `filesystem` attribute on `FakePath` that
is written by `init_module()`. The Shape 6 fix moved `FakePathModule` separator attrs to
per-instance; the same treatment is needed for `FakePath`.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| C.1 | `fake_pathlib.py:63` | `FakePath.filesystem = filesystem` is a class-level write without synchronisation; if two `Patcher` instances are active concurrently (per-thread patchers), the second `init_module()` overwrites the first. `pathlib.Path()` constructions in the first thread then resolve against the wrong filesystem. | redesign |
| C.2 | `fake_pathlib.py:596` | `FakePath.skip_names` is a class-level mutable list. *Speculation: write path not confirmed in current code; flagged because the pattern is identical to C.1.* | redesign |

**Why redesign:** C.1 cannot be locked away. The class attribute is read on every `pathlib.Path()`
construction by `FakePath.__new__`; adding a lock to `__new__` would serialise all `Path()`
constructions across all threads. The structural fix is to make `filesystem` per-instance state on
the `FakePathlibModule` instance and have `FakePath.__new__` resolve it through the active patcher
context (§5).

### Cluster D: `FakeShutilModule` class-level state

**Common cause:** `module_lock` is class-level and shared across `Patcher` instances; `_patch_level`
is per-instance. The two are not coordinated.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| D.1 | `fake_filesystem_shutil.py:53` | `module_lock = RLock()` is a class attribute; all `FakeShutilModule` instances share it. `_patch_level` is per-instance. Two instances can each acquire `module_lock`, mutate `shutil._HAS_FCOPYFILE` global, and increment their own `_patch_level` — but the global restore in `_stop_patching_global_vars` uses one instance's `_patch_level`, so the other instance's nesting is wrong. | redesign |
| D.2 | `fake_filesystem_shutil.py:55-60` | Class attributes capture real `shutil` module state at *import time*, not patch time. Restoring on stop uses the import-time snapshot, which can be wrong if another module patched `shutil` between import and pyfakefs activation. | locking |

### Cluster E: `__getattr__`-dispatched I/O wrappers bypass the lock

**Common cause:** `FakeFileWrapper.__getattr__` returns closures or runs pre-dispatch code
(`_sync_io`, `flush`) that touches `_read_seek`, `_read_whence`, `_io`, and
`file_object._byte_contents` outside any lock.  The `_with_lock` auto-wrapping pattern only covers
`FakeFilesystem` public methods, not `FakeFileWrapper.__getattr__` indirection.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| E.1 | `fake_file.py:1277-1283` | `__getattr__` returns `_read_wrappers(name)`, `_other_wrapper(name)`, `_write_wrapper(name)` — closures that mutate `_read_seek`, `_read_whence`, `_io` without `_lock`. Two threads doing concurrent reads on the same append-mode wrapper produce torn seeks. | locking |
| E.2 | `fake_file.py:1269-1274` | The same `__getattr__` path calls `self._sync_io()` and `self.flush()` *before* returning the closure; these run unlocked, so a concurrent write between `_sync_io()` and the actual read returns stale content that `_sync_io` believed was current. | locking |
| E.3 | `fake_file.py:1317-1322` | `_is_open()` reads `len(self.filesystem.open_files)` and indexes `open_files[self.filedes]` without `_lock`. Concurrent `close_open_file()` can null out `open_files[fd]` between length and index, returning `True` for a just-closed fd. | locking |

**Why these are lock-shaped, not redesign-shaped.** Cluster E is patchable in place: the closures
and `_is_open()` need to acquire `_lock`. The fix is uncomplicated. We flag it as a cluster because
the auto-wrapping pattern's blind spot — `__getattr__` indirection — implies similar gaps elsewhere
that the retrofit's static analysis missed. Anywhere a `FakeFile*` method is called via attribute
lookup or descriptor protocol rather than direct call, the auto-wrapping does not apply.

### Cluster F: Process-global mutation outside any lock

**Common cause:** pyfakefs mutates true process-global state — module attributes on `tempfile` and
`sys` — that are not owned by the `FakeFilesystem` and therefore cannot be lock-protected by it.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| F.1 | `fake_filesystem.py:3365`, `fake_filesystem_unittest.py:135` | `tempfile.tempdir = None` written without any lock. Concurrent `tempfile.mkdtemp()`/`gettempdir()` reads observe a partially-written value under free-threaded CPython, or the wrong value under stock CPython if the writer is not the active patcher. | accept-and-document |
| F.2 | `fake_filesystem_unittest.py:1092, 1098, 1202` | `sys.meta_path.insert(0, ...)` and `pop(0)` are not atomic with respect to concurrent imports. If another finder is inserted between insert and pop, `pop(0)` removes the wrong finder. | locking |

**Why F.1 is accept-and-document.** `tempfile.tempdir` is process-global; the only correct fix is to
not mutate it, or to document that pyfakefs is incompatible with concurrent `tempfile` use. Locking
it within pyfakefs does not help — code outside pyfakefs reads the attribute without that lock.

**F.2 is fixable.** Use `sys.meta_path.remove(self._dyn_patcher)` instead of `pop(0)`, and serialise
the insert/remove pair with a module-level lock that `importlib`'s import lock co-ordinates with.

### Cluster G: `os.setter` / `reset()` mutates live filesystem under the lock

**Common cause:** `FakeFilesystem.os.setter`, `is_windows_fs.setter`, and `reset()` replace the
filesystem's root, mount points, and open-file table. The `_with_lock` decoration makes these atomic
with respect to other locked operations, but does not invalidate references that callers already
hold.

| ID | Site | Failure mode | Fix register |
|----|------|--------------|--------------|
| G.1 | `fake_filesystem.py:457-468` | `reset()` replaces `self.root`, `self.open_files`, `self.mount_points`. Threads holding `FakeFile` references obtained pre-reset see a node that is no longer in the tree; subsequent ops succeed structurally but are invisible to the new filesystem. | accept-and-document |
| G.2 | `fake_filesystem.py:412-421` | `cwd` setter is a property setter, **not wrapped by `_with_lock`** (the loop only matches `inspect.isfunction`). Concurrent `os.chdir()` and path resolution race on `_cwd` reads under free-threaded CPython. | locking |
| G.3 | `fake_filesystem.py:276-277, 2734` | `umask` is read in `create_file_internally` and written by `FakeOsModule.umask()` without `_lock`. Concurrent `os.umask(mode)` and `os.open()` produces files with wrong permissions. | locking |

**G.2 is the auto-wrapping blind spot.** PLAN.md O2 noted that the `_with_lock` loop wraps
contextmanager methods redundantly; the more serious blind spot is property setters, which the loop
does not match at all. Audit every property setter for thread-safety; either extend the loop to
cover them, or add explicit `with self._lock:` blocks.

### Cluster summary

| Cluster | Hazards | Fix register |
|---------|---------|--------------|
| A. Patcher lifecycle | 4 | 2 redesign + 2 locking |
| B. Callback under lock | 3 | 2 redesign + 1 locking |
| C. `FakePath` class state | 2 | redesign |
| D. `FakeShutilModule` class state | 2 | 1 redesign + 1 locking |
| E. `__getattr__` blind spot | 3 | locking |
| F. Process globals | 2 | 1 accept + 1 locking |
| G. `reset()` / setters | 3 | 1 accept + 2 locking |
| **Total** | **19** | **8 redesign, 9 locking, 2 accept-and-document** |

Roughly half the new findings are patchable with finer locking and could ship as follow-ups to the
retrofit; the other half require architectural change that this report develops in §4 and §5.

---

## §3. The structural problem

The hazards in §2 are not a list of bugs. They are consequences of three architectural choices that
pyfakefs made early and that constrain everything downstream. Each choice creates a class of hazards
that no amount of locking eliminates.

### 3.1 The `Patcher` is a process-wide refcounted singleton

`Patcher.__new__` returns the singleton `PATCHER` if one exists, otherwise creates one. `setUp`
increments `REF_COUNT`; `tearDown` decrements. The *intent* is that test frameworks creating one
`Patcher` per test co-ordinate via the refcount: nested `Patcher()` calls (e.g., a fixture inside a
fixture) reuse the same patched modules.

The cost of this design is that all `Patcher` instances in the process share state. Two pytest
workers in the same process (`pytest-xdist` with `--dist=loadgroup` running threads, or a custom
test harness that parallelises within a process) cannot have independent `FakeFilesystem` instances
— the second worker's `Patcher()` returns the first worker's singleton, with the first worker's
filesystem.

The retrofit's `_class_lock` serialises the refcount and module-cache operations, which addresses
the immediate races. It does not address the underlying single-instance constraint. Hazards A.1,
A.2, C.1, D.1 all trace to this constraint: there is exactly one `FakeFilesystem`, exactly one
`FakePath.filesystem`, exactly one `module_lock`, so concurrent patcher activations have to merge
into the existing state rather than creating their own.

A redesign that separates "the lock over patched module state" (must be process-wide) from "the
active fake filesystem" (should be per-test) removes the constraint. The lock stays a singleton; the
filesystem becomes a `contextvars`-scoped value (§5.2).

### 3.2 The fake filesystem is a mutable shared graph

`FakeFilesystem.root` is a `FakeDirectory` whose `entries` dict points to `FakeFile`/`FakeDirectory`
children. The graph is mutated in place by `create_file`, `remove_object`, `rename`, `chmod`, etc.
The retrofit's `RLock` serialises mutation, but mutation is still in-place: every `create_file`
writes to the same dict that every `listdir` reads.

The cost of in-place mutation is the entire Cluster B and Cluster E hazard surface. Live-reference
returns (`get_object`, `entries`, `root_dir`) are necessary for performance — copying every result
would make `listdir` of a large directory unbearable — but they require callers to hold `fs.lock()`
across multi-step operations, which is a contract not a guarantee. The retrofit documented the
contract in `fs.lock()` and the README; it cannot enforce it.

The structural alternative is **immutable filesystem snapshots with copy-on-write mutation.** A
`FakeFilesystem` becomes a value, not an object. `create_file` returns a new `FakeFilesystem` with
the new file added; the old filesystem is unchanged. Concurrent readers see a consistent snapshot;
concurrent writers race for the "current" pointer.  The pattern is the standard MVCC (multi-version
concurrency control) approach used by every modern database.

This is a substantial change. We do not propose it as a near-term step, but we name it because it is
the only architecture in which Cluster B and Cluster E hazards do not exist by construction.

### 3.3 Monkey-patching is the integration model

pyfakefs activates by patching `sys.modules['os']` (and `os.path`, `io`, `pathlib`, etc.) with
`FakeOsModule` instances. The patches are process-wide: every thread in the process sees the patched
modules. The `Patcher.pause()` / `resume()` mechanism toggles patching globally.

The cost of process-wide patching is that pyfakefs cannot offer per-thread isolation at all. Every
thread sees the same `os.open`. If a test wants to spawn a worker thread that uses the real
filesystem while the test thread uses the fake, pyfakefs does not support this — the worker inherits
the patched `os` module. Hazard F.1 (`tempfile.tempdir`) is the explicit symptom; the implicit
symptom is that the entire `threading.local` strategy in `helpers._id_thread_state` and
`FakeOsModule.use_original` is a workaround for the lack of per-thread isolation.

The structural alternative is **`contextvars`-rooted module substitution.** Instead of replacing
`sys.modules['os']` with a fake, patch `os.open` (and friends) with a thin dispatcher that reads the
active filesystem from a `ContextVar`. Threads that have not entered a `Patcher` context see the
real `os.open`; threads that have see their context's fake. This pattern is how
`decimal.localcontext`, asyncio's event loop, and structured-concurrency libraries achieve
thread/task-local module-like behaviour without `sys.modules` mutation.

The migration cost is real (every patched function gains a one-line dispatcher), but it makes hazard
clusters C, D, F, and the entire "per-thread shim" pattern in `helpers.py` and `fake_os.py`
obsolete.

### 3.4 The API-shape problem: locking cannot reach what the API gives away

The three architectural choices above each create a class of hazards.  This section describes the
class that subsumes the others — the reason the retrofit's locking-only approach cannot reach thread
safety, no matter how comprehensive the locking becomes. We treat it last because its statement
requires the vocabulary of §3.1–§3.3, but it is the report's load-bearing claim and the immediate
motivation for the redesign in §5.

**The claim.** Locking is sound only at the lock's *boundary*. Inside the lock, invariants hold;
outside, they do not. A library that guarantees thread safety must therefore ensure that no value
crosses the lock boundary outward in a form that allows the caller to reach back into shared state.
pyfakefs's public API does exactly that, by construction:

- `fs.get_object(path)` returns a `FakeFile` whose `_byte_contents`, `st_mode`, `st_uid`, etc. are
  mutable and shared with every other caller who resolves the same path.
- `fs.root_dir` returns the `FakeDirectory` that *is* the root of the in-memory tree.
- `directory.entries` returns the live `_entries: dict[str, FakeFile]` mapping — not a copy.
- `fs.scandir(path)` and `fs.walk(path)` yield live `FakeFile` and `FakeDirectory` nodes whose
  attributes the caller can read or write directly.
- `open(path)` returns a `FakeFileWrapper` whose `__getattr__` reaches into
  `file_object._byte_contents`, `file_object.epoch`, and `filesystem.open_files` on every operation
  (Hazard cluster E).
- `pathlib.Path(p).stat()` returns a fresh value (good), but `pathlib.Path(p)` itself is a
  `FakePath` whose `filesystem` attribute is shared class-level state (Hazard cluster C).

Each of these is a deliberate API choice that predates thread safety as a goal, and each is
reasonable in single-threaded use: callers expect `get_object` to return *the* file, not a snapshot,
because mutations to the returned object should be visible in subsequent reads. The library's
single-threaded contract is **reference semantics**: the filesystem is a graph, the API hands you
nodes from the graph, and you mutate the graph by mutating the nodes.

Reference semantics are incompatible with locking. The library cannot hold its lock while the caller
holds the reference — that would serialise every test that ever calls `get_object`. So the lock is
released at return. After return, two outcomes are possible:

1. The caller mutates the reference inside their own `with fs.lock():` block, in which case the
mutation is safe.
2. The caller mutates the reference without acquiring the lock, in which case the mutation races
with every concurrent caller of any `FakeFilesystem` method.

The retrofit documented option 1 as the contract. It cannot enforce it.  Worse, the language and the
API surface tempt users into option 2:

```python
f = fs.get_object('/foo/bar')
f.contents = b'new'
```

Three things are true about this snippet:

1. **It looks correct.** It is the obvious way to mutate a file's contents. The tutorial examples in
   `docs/usage.rst` use this pattern. The library's own internal code uses it.
2. **It races.** No lock is held during the assignment; `_byte_contents` is being written while
   another thread may be reading it.
3. **The user has no way to know.** Nothing in the type signature, the return value's repr, the
   docstring of `get_object`, or the runtime behaviour distinguishes a "live reference that escapes
   the lock" from a "snapshot that is safe to use." Both look like Python objects with attributes.

The retrofit added documentation to `fs.lock()` and to the README's Thread safety section explaining
the contract. Documentation is the weakest available enforcement mechanism, and this contract is
unusually hard to enforce because the unsafe pattern is the *natural* pattern.  Users do not write
the snippet above as an oversight; they write it because every Python tutorial has trained them to
mutate object attributes directly, and the library's API suggests that this is how pyfakefs works.
The library *is* this API: the only way to interact with the filesystem at the `FakeFile` level is
through reference mutation.

**The auto-locking pattern compounds the problem.** The retrofit's `inspect.getmembers` loop wraps
every `FakeFilesystem` public method in `_with_lock`. From the caller's perspective, every call to
`fs.create_file(path)`, `fs.stat(path)`, `fs.get_object(path)` is thread-safe — and each call, in
isolation, *is* thread-safe. The lock covers the call's body. The auto-wrapping creates a uniform
surface that is correctly described as "every public method of `FakeFilesystem` acquires `_lock`."

What the auto-wrapping does *not* describe is that `fs.get_object(path)` returns a value that
escapes the lock at return time. The caller sees a thread-safe call that returns an object; the
caller reasonably generalises to "objects returned from a thread-safe library are safe to use"; the
caller is wrong, but the library has given them no signal.  This is the false floor: the retrofit
makes the library *appear* thread-safe at the boundary that users see (the call surface), while
remaining unsafe at the boundary that matters (the object lifetime beyond the call).

We have audited this pattern in the post-retrofit code. The audit found 12 distinct public methods
that return live mutable references (`get_object`, `lresolve`, `resolve`, `root_dir`,
`mount_points`, `open_files`, `entries`, `scandir`, `walk`, `add_real_file`, `create_file`,
`create_dir`, `create_symlink`). Each is documented in the `fs.lock()` docstring as requiring `with
fs.lock():` for safe mutation. Each is, in the post-retrofit code, locked *during the call* and
unlocked *after return*. The 12 documented sites are the visible surface; the audit also found
`__getattr__`-dispatched returns from `FakeFileWrapper` that are not documented at all (Cluster E).

**No amount of additional locking closes this.** Adding finer-grained locks (per-node, per-wrapper,
per-fd) does not help: it just multiplies the boundaries that values escape across. The lock at any
granularity is held during the call and released at return; the returned reference escapes
regardless. Adding contention-aware optimisations (read-write locks, lock-free reads of immutable
fields) does not help either: the *hazard* is that mutable state is reachable from the returned
reference, and lock-free reads do not constrain mutable writes through that reference.

The only structural fix is to **change what the API returns**. Three patterns work, in increasing
order of API disruption:

- **Snapshot at return.** `get_object(path)` returns a frozen copy of the `FakeFile` (a dataclass
  with the same field shape, immutable).  Callers who need to mutate must use a separate write API
  (`set_contents`, `chmod`, etc.) that re-resolves the path under the lock. This is what `os.stat`
  already does — `stat_result` is effectively immutable.

- **Opaque handle.** `get_object(path)` returns a `FakeFileHandle` that carries the path and a
  generation counter. Operations on the handle (`handle.contents`, `handle.set_contents(b'new')`)
  re-resolve the path under the lock at every call, validating the generation counter to detect
  deletion or replacement. The handle never directly references the underlying `FakeFile`. This
  pattern matches `pathlib.Path` — `Path('/foo')` is a value; `Path('/foo').read_bytes()` re-opens.

- **Mutator closure.** `fs.with_object(path, lambda f: f.set_contents(b'new'))` takes a callback
  that runs under the lock with the live reference available. The reference cannot escape the
  closure (modulo the user capturing it, which we cannot prevent but can document as unsupported).
  This is functional in style; it is the safest but the most unfamiliar to users of the current API.

The §5 redesign proposes the **handle** pattern as the primary path, with snapshots for read-only
queries. We do not propose a mutator- closure API — too unfamiliar — but we mention it because it is
the only pattern in which the lock-escape hazard is structurally impossible.

**Why this is the absent-abstraction-layer problem, not just a bad-API problem.** Snapshot returns
and opaque handles are not three independent clever ideas; they are the natural shape of *any*
userspace interface to a kernel-equivalent. A real filesystem has the same problem pyfakefs has —
concurrent processes mutating shared state — and solves it by having the kernel own the locking and
exposing only values and opaque handles to userspace. Userspace gets:

- `int fd = open(path, ...)` — opaque handle
- `struct stat st; fstat(fd, &st)` — value (stat is a struct, copied out)
- `read(fd, buf, n)` — value (bytes copied out of the page cache)
- `write(fd, buf, n)` — value (bytes copied into the page cache)

Userspace never gets a pointer to a kernel `struct inode`. The kernel locks inode and dentry
structures during the syscall body and copies the result out before returning. There is no "live
reference" that escapes the syscall boundary; the boundary is the *only* point of interaction.

pyfakefs's `FakeOsModule.open()`, `FakePathModule.exists()`, and similar are conceptually syscalls —
they translate user-level calls into filesystem operations. They have the right shape for thread
safety. The problem is that pyfakefs *also* exposes `fs.get_object(path)`, `directory.entries`, and
`fs.root_dir`, which are the equivalent of returning `struct inode *` to userspace. There is no
real-OS equivalent of these calls because no real OS would expose them. The retrofit's locking
cannot save them because the underlying abstraction does not exist: there is no fake-VFS layer
between the public API and the in-memory state to host the locking.

The redesign in §5 is therefore not "patch the leaky API surface" — it is "introduce the abstraction
layer that should have been there from the start." The snapshot/handle/edit shapes follow naturally;
they are what any correctly-layered filesystem fake exposes.

**The implication for the retrofit's status.** The retrofit on `fix-thread-races` is a real
improvement, and we want it merged. But its public framing — that pyfakefs is now thread-safe
through the public API — needs to be qualified. Concretely:

- Calls to `FakeFilesystem` methods are thread-safe in isolation.
- Holding and using the returned `FakeFile`/`FakeDirectory` references across calls is *not*
  thread-safe, and cannot be made thread-safe without introducing the missing abstraction layer.
- The README's current claim ("pyfakefs is thread-safe for code that uses the public APIs") is true
  for the call surface but misleading for users who follow the documented patterns of holding
    `FakeFile` references. The README should say so explicitly until the redesign ships.

This is the load-bearing claim of the report. Every concrete proposal in §5 follows from it: the
redesign is not "the current code is bad" — the current code is good for the abstraction it
implements — it is "the abstraction is missing a layer, and adding that layer is what makes thread
safety reachable."

---

## §4. Open architectural concerns from the retrofit

Three concerns surfaced during the retrofit that PLAN.md flagged as observations rather than action
items. The redesign needs to address them.

### 4.1 The auto-wrapping `_with_lock` pattern (PLAN.md O2, O3)

The `inspect.getmembers` loop at `fake_filesystem.py:3401` wraps every public method of
`FakeFilesystem` with `_with_lock`. The loop is load-bearing for the retrofit's correctness — it is
how the lock got applied to ~80 methods without per-method audit — but it has three weaknesses:

1. **It wraps `@contextmanager` methods redundantly** (`lock()`, `use_fs_type()`). The wrapper
   acquires-then-releases the lock before the generator runs; the generator's own `with self._lock:`
   re-acquires during yield. Correct but confusing; future maintainers will read the pattern and
   assume it's wrong.
2. **It does not wrap property setters** (Hazard G.2). The loop matches `inspect.isfunction` only.
3. **It auto-locks future methods.** Any method added to `FakeFilesystem` after the retrofit will be
   auto-locked without the author thinking about whether the lock is appropriate — the
   safe-by-default behaviour is good for routine methods and wrong for methods that should run
   unlocked (pure getters, methods that hold the lock via a contextmanager, methods that
   intentionally release it for callbacks).

The redesign should make locking explicit per-method via a decorator, with a static check (extension
to `test_thread_safety_static.py`) that flags any unmarked public method. Explicit-locking trades
off boilerplate for design visibility.

### 4.2 Callback re-entry

Cluster B documents the immediate hazards. The structural concern is that pyfakefs's public surface
includes callback parameters (`side_effect`) and lazy-load property bodies that conceptually invoke
user code while the library holds its lock. The contract "do not acquire any lock in your
side_effect" is not a contract a library can enforce.

The retrofit chose to hold the lock across callbacks; the redesign should either:

1. **Defer callbacks** until after lock release, queuing them on a per-filesystem callback queue
   drained at lock-release time.
2. **Eliminate callbacks** from inside locked sections by moving the side-effect-triggering points
   outside the lock.
3. **Document the contract explicitly** and accept that violations are user error.

Option 3 is the cheapest and what the retrofit implicitly chose. Option 1 is the most robust but has
ordering implications (a callback queued from `create_file` may run after a subsequent
`delete_file`). Option 2 is case-by-case work.

### 4.3 Free-threaded CPython readiness

PEP 703 free-threaded CPython removes the GIL. Several hazards in §2 are GIL-masked: G.2 (`cwd` torn
read), G.3 (umask race), F.1 (`tempfile.tempdir` torn write). Stock CPython 3.10–3.13 will not
exhibit them; CPython 3.13t and 3.14+ (where free-threaded is the default) will.

The audit for free-threaded readiness is mechanically the same as the audit for any-CPython
readiness: every shared mutable read/write must be either atomic by construction (immutable
assignment of a Python object reference is atomic at the GIL level and remains atomic under PEP 703
due to the per-object lock) or protected by a synchronisation primitive. The retrofit's `RLock`
covers the latter. The gaps are exactly the hazards in §2.

The static check in `test_thread_safety_static.py` should be extended with categories for
free-threaded-specific hazards: `int +=` on shared attributes (already covered as Shape 1), `str =`
to module globals (Shape 7), `dict[k] = v` outside a lock (not yet covered), and the property-setter
blind spot (G.2).

---

## §5. North-star design: a layered fake kernel

The redesign mirrors the structure a real OS uses to give userspace atomic syscall semantics on
shared state: layered, with locking internal to each layer, exposing only values and opaque handles
at each upward boundary. pyfakefs serves a different workload than a real kernel — a few thousand
operations per test on a few hundred fake files, not billions of operations on millions of inodes —
so each layer can use coarser locking than its real counterpart while preserving the correctness
properties. **Coarser locking inside the abstraction is the right engineering trade-off** for a
test-helper library: the maintenance budget is small, the worldwide userbase needs the library to
stay small, fast, and reliable, and the workload does not justify per-inode locks or RCU.

### 5.1 The layered architecture

Three layers, with the locking domain explicit at each:

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 3: patched module surface                                │
│  FakeOsModule, FakePathModule, FakePathlibModule, FakeIoModule  │
│                                                                 │
│  Translates user calls into fake-VFS operations.                │
│  Owns: nothing mutable.  Locks: none (stateless dispatchers).   │
│  Exposes upward: real Python types (int fds, stat_result,       │
│    bytes, str, FakePath value objects).                         │
└────────────────────────────┬────────────────────────────────────┘
                             │ fake-VFS API (values & handles)
┌────────────────────────────┴────────────────────────────────────┐
│  Layer 2: fake-VFS                                              │
│  FakeVFS                                                        │
│                                                                 │
│  Implements filesystem operations: open, read, write, lookup,   │
│  create, unlink, rename, stat, chmod, chown.                    │
│  Owns: open-file table, dentry/path cache.                      │
│  Locks: one coarse RLock for the whole VFS.                     │
│  Exposes upward: opaque file descriptors, value-typed results.  │
└────────────────────────────┬────────────────────────────────────┘
                             │ inode-table API (internal)
┌────────────────────────────┴────────────────────────────────────┐
│  Layer 1: in-memory state                                       │
│  FakeInodeTable, FakeFile, FakeDirectory, _byte_contents        │
│                                                                 │
│  The actual filesystem graph in memory.                         │
│  Owns: every FakeFile, every FakeDirectory, every byte of       │
│    file content, the mount-point map, the inode-allocation      │
│    counter.                                                     │
│  Locks: none directly (its callers from Layer 2 hold the VFS    │
│    lock).                                                       │
│  Exposes upward: nothing public.  Internal types only.          │
└─────────────────────────────────────────────────────────────────┘
```

The current `FakeFilesystem` class collapses Layers 1 and 2 into a single object and exposes its
internals directly to the patched modules in Layer 3 (and via `fs.get_object`, to user code). The
redesign separates them.

**Why three layers, not two.** The patched modules in Layer 3 must remain stateless dispatchers —
they are how user code reaches pyfakefs, and they must support `pause`/`resume`, per-context
filesystem selection, and the "spawn a worker that uses the real OS" pattern without owning state
themselves. The fake-VFS in Layer 2 is the natural home for the lock and the open-file table. The
in-memory state in Layer 1 is what the lock protects. Collapsing Layers 1 and 2 (as the current code
does) is what creates the API-shape problem in §3.4: there is no separate "VFS" surface to expose to
Layer 3, so internal types leak through.

### 5.2 Layer 2's locking policy: one coarse RLock, no escape

The fake-VFS holds a single `threading.RLock`. Every public method on `FakeVFS` acquires the lock on
entry, performs its work on Layer 1 state, **copies any results that need to leave the lock into
value types**, releases the lock, and returns the values.

Sketch:

```python
class FakeVFS:
    def __init__(self):
        self._lock = threading.RLock()
        self._inodes = FakeInodeTable()       # Layer 1
        self._open_files = FakeOpenFileTable()  # Layer 2 internal
        self._dentry_cache = {}                # Layer 2 internal

    def stat(self, path: str) -> os.stat_result:
        with self._lock:
            inode = self._inodes.lookup(path)
            return _stat_result_for(inode)  # value-typed; safe to leak

    def open(self, path: str, flags: int, mode: int = 0o666) -> int:
        with self._lock:
            inode = self._inodes.lookup_or_create(path, flags, mode)
            return self._open_files.allocate(inode, flags)  # int fd; opaque handle

    def read(self, fd: int, n: int) -> bytes:
        with self._lock:
            of = self._open_files.get(fd)
            data = of.inode._byte_contents[of.pos : of.pos + n]
            of.pos += len(data)
            return bytes(data)  # copy; safe to leak

    def write(self, fd: int, data: bytes) -> int:
        with self._lock:
            of = self._open_files.get(fd)
            of.inode.append_bytes(data)  # mutation under lock
            return len(data)
```

The pattern is uniform: lock, mutate Layer 1, copy results out, unlock, return. Nothing crosses the
boundary except values and opaque integer handles. The lock is held during the call body and
released at return, exactly as the retrofit does — but the *return values* are no longer references
into shared state, so the lock-escape problem from §3.4 does not exist.

**Why one coarse lock, not per-inode.** Real kernels use per-inode and per-page locks because they
have throughput requirements pyfakefs does not have. A test that creates 500 fake files and runs a
handful of threads across them is not contention-bound; the lock-acquire/release overhead dominates
the lock-wait time. A single RLock is simpler to reason about, simpler to maintain, and adequate for
the workload.  Should a future user demonstrate genuine contention in a real test suite, per-inode
locking can be retrofitted *inside Layer 2* without changing the upward API — which is exactly the
encapsulation property we want.

**The static check from `test_thread_safety_static.py` enforces the discipline.** Every public
method on `FakeVFS` must acquire `self._lock` in its body. Methods that legitimately do not (pure
stateless utilities) opt out via a `# thread_safe_ok: <reason>` comment. The check that already
catches `last_ino += 1` is extended to catch unwrapped public methods.

### 5.3 Layer 3's syscall API: values, fds, and handles

The patched modules in Layer 3 dispatch user calls into Layer 2 and translate the results back into
the types that user code expects. The translation is mechanical:

- `os.open(path, flags, mode)` → `vfs.open(path, flags, mode)`, returns the integer fd directly
  (opaque handle).
- `os.stat(path)` → `vfs.stat(path)`, returns the `os.stat_result` value directly.
- `open(path).read()` → `vfs.open(...)` then `vfs.read(fd, n)`, returns the `bytes` value directly.
- `pathlib.Path(p).read_bytes()` → same path-construction-then-read pattern; `FakePath` is a value
  object that carries the path string and dispatches to the active VFS on each operation.

There is no `fs.get_object(path)` in Layer 3, because there is no type for it to return. Layer 1's
`FakeFile` is internal. Userspace gets values and fds, which is what real userspace gets from a real
kernel.

For test code that needs to inspect or manipulate filesystem state that the standard syscalls do not
expose (uncommon, but it happens — e.g., setting `st_uid` on a created file), Layer 2 exposes a few
extension methods:

```python
def get_attrs(self, path: str) -> FakeFileAttrs:
    """Return a value-typed snapshot of all attributes."""

def set_attrs(self, path: str, *, st_mode=None, st_uid=None, ...) -> None:
    """Atomically set zero or more attributes."""

@contextmanager
def edit(self, path: str) -> Iterator[FakeFileEditor]:
    """Atomic multi-step editor; locks for the duration of the with-block."""
```

`FakeFileAttrs` is a frozen dataclass — readable but not writable.  `FakeFileEditor` is a thin proxy
that provides setter methods; the underlying inode is held by the editor inside the `with` block,
the VFS lock is held throughout, and the editor is invalid after the block exits. This is the
snapshot/handle/edit shape from the previous draft, but framed as Layer 2 extension methods rather
than a replacement for Layer 1 references.

**The `FakeFile` and `FakeDirectory` types stop being public.** They are removed from the documented
API, the `__all__` of `fake_filesystem`, and the type annotations of public methods. Code that
imported them directly was relying on undocumented internals; the deprecation cycle in §6 gives
those callers a migration window.

### 5.4 The patcher boundary: process-wide module patches, per-context active VFS

The `Patcher` continues to own the `sys.modules` patching — that is necessarily process-wide. What
changes is that the *active* fake VFS is selected by a `contextvars.ContextVar`, not by a singleton.

```python
_active_vfs: ContextVar[FakeVFS | None] = ContextVar("pyfakefs_active_vfs", default=None)

def get_active_vfs() -> FakeVFS | None:
    return _active_vfs.get()

class Patcher:
    """Process-wide module patcher with per-context active VFS."""

    _module_lock = threading.Lock()
    _module_refcount = 0
    _stubs = None

    def __init__(self):
        self.vfs = FakeVFS()

    def __enter__(self):
        self._token = _active_vfs.set(self.vfs)
        with Patcher._module_lock:
            if Patcher._module_refcount == 0:
                Patcher._stubs = _install_module_patches()
            Patcher._module_refcount += 1
        return self.vfs

    def __exit__(self, *exc):
        with Patcher._module_lock:
            Patcher._module_refcount -= 1
            if Patcher._module_refcount == 0:
                _restore_module_patches(Patcher._stubs)
                Patcher._stubs = None
        _active_vfs.reset(self._token)

# Patched module dispatcher
def fake_os_open(path, flags, mode=0o777):
    vfs = get_active_vfs()
    if vfs is None:
        return _real_os.open(path, flags, mode)
    return vfs.open(path, flags, mode)
```

Two locks, both narrow:

- `Patcher._module_lock`: held only during `_install_module_patches` / `_restore_module_patches`.
  Never held across a VFS operation, never held across user code, never held across `__del__`
  finalizers. Its hold time is bounded by the number of patched modules.
- `FakeVFS._lock`: held only inside Layer 2 method bodies. Never held across user code, never held
  across `Patcher` lifecycle.

The two locks have no ordering relationship because they are never held together. Lock cluster A
from §2 dissolves: there is no patcher lifecycle window in which `_module_lock` is released between
phases, because the lifecycle is just `_install` and `_restore`, each single-shot under the lock.

`contextvars` give us per-thread and per-asyncio-Task isolation naturally: each test runs in its own
context, `_active_vfs` is set on entry and reset on exit, and concurrent tests in different threads
see different VFSes without any explicit `threading.local` plumbing. The "spawn a worker that uses
the real OS" pattern works out of the box: `_active_vfs.set(None)` inside the worker, and
`fake_os_open` falls through to the real `os.open`.

### 5.5 Callback discipline and FT invariants

Because Layer 2 never holds its lock across user code, the callback- under-lock hazards in Cluster B
disappear by construction. The specific changes:

- **`FakeFile._side_effect`** is invoked by Layer 1 internal code that mutates inode contents. Move
  the invocation to *after* the Layer 2 method returns, by queuing the side effect on the VFS and
  draining the queue in `FakeVFS.write`'s tail (after lock release).
- **Lazy real-directory loading** (`FakeDirectoryFromRealDirectory`) becomes eager: when a real
  directory is mounted, its contents are walked once at mount time, under the VFS lock, and the
  resulting inode tree is pinned. No more `os.listdir` calls inside the lock.
- **`__del__` finalizers** on Layer 1 types do not acquire any lock.  Garbage collection of a
  `FakeFile` is a pure-Python free; the inode is already unreachable.

Free-threaded CPython invariants are documented and enforced by the static check:

- Layer 2 public methods acquire `_lock`. (Static check: every public `FakeVFS` method either calls
  `with self._lock:` or is marked `# thread_safe_ok:`.)
- Layer 1 types are accessed only from inside `_lock`. (Static check: pattern grep for
  `FakeFile`/`FakeDirectory`/`FakeInode` references in code outside `pyfakefs.fake_vfs`.)
- No `+=` on shared module globals or class attributes (Shape 1, Shape 7). (Existing static check
  covers this.)
- Process-global mutation (Cluster F: `tempfile.tempdir`, `sys.meta_path`) is contained:
  `tempfile.tempdir` is patched once at module-patch install, restored once at uninstall, and read
  through `vfs.tempdir` from inside the VFS otherwise.

### 5.6 Strategic simplifications appropriate to a test helper

Real kernels do many things pyfakefs deliberately does *not* do.  Naming the simplifications
explicitly so the maintainer can confirm they are acceptable:

- **One coarse VFS lock instead of per-inode/per-dentry locks.** Pyfakefs does not have throughput
  requirements that justify fine-grained locking; the workload is bounded by test count, not by
  filesystem traffic.
- **No journal, no fsync semantics, no crash-consistency model.** Tests do not crash mid-operation;
  if they do, the test is broken, not pyfakefs.
- **No page cache, no inode cache, no dentry cache eviction.** Pyfakefs stores everything in memory
  at all times; eviction is unnecessary.
- **No RCU, no read-copy-update for read-mostly workloads.** Concurrency in tests is bounded;
  reader-side optimisation is not worth the complexity.
- **No fork safety beyond document-and-warn.** A test that calls `os.fork()` while pyfakefs is
  active is doing something unusual enough that "consult the docs" is an acceptable answer.
- **No async-native VFS API.** `contextvars` give per-Task isolation, but Layer 2 methods stay
  synchronous because the patched syscalls (`os.open`, `os.read`) are synchronous. Async callers
  that block on Layer 2 will block their event loop, which is exactly what they would do if calling
  the real `os.open` from async code.

These simplifications are what keep pyfakefs small, fast, and reliable. The redesign preserves all
of them.

### 5.7 What this redesign explicitly does NOT propose

- **Immutable copy-on-write filesystem (§3.2).** Worth mentioning as a future direction but a 6-12
  month project; out of scope for the migration in §6. The layered design in §5.1 does not preclude
  it — Layer 1 could become a persistent data structure later without changing the Layer 2 API.
- **Removing monkey-patching.** The `sys.modules` patching is load-bearing for the "no test code
  modification" property; we are not proposing to remove it, only to make the active VFS visible
  through `contextvars` rather than through a singleton.
- **Async-native API.** Out of scope, as noted in §5.6.
- **A pluggable VFS interface.** Layer 2 is `FakeVFS`, full stop; we do not propose abstracting it
  for alternate implementations.

---

## §6. Phased migration that respects back-compat

The migration introduces the layered architecture from §5 incrementally.  Phase A is an internal
refactor with no API change (the layering becomes real, but only inside the library). Phase B
exposes the new layering and deprecates the leaky surface. Phase C, in a major version, removes the
deprecated surface and completes the encapsulation.

The principle: **the layered architecture is built first as implementation, then surfaced as API**,
not the other way around. This avoids the failure mode where new and old APIs co-exist for years
without internal coherence.

### Phase A: introduce the fake-VFS layer internally + ship lock-shaped fixes — minor version bump

Goal: build Layers 1-3 as an internal refactor of the current `FakeFilesystem`, with no observable
API change.

- **Refactor** `FakeFilesystem` into the three-layer structure:
  - Layer 1: `FakeInodeTable`, `FakeFile`, `FakeDirectory` move into a new `pyfakefs._inode` module.
    Marked private with leading underscore. The types are still importable from the public
    `pyfakefs.fake_filesystem` for back-compat, but the canonical location is the private module.
  - Layer 2: a new `FakeVFS` class lives inside `FakeFilesystem` as `self._vfs`. All filesystem
    operations (`open`, `read`, `write`, `stat`, `lookup`, `create`, `unlink`) are implemented on
    `FakeVFS` against `_inode` types. `FakeFilesystem` becomes a thin facade that delegates to
    `self._vfs` and preserves the current public methods.
  - Layer 3: `FakeOsModule`, `FakePathModule`, `FakePathlibModule`, `FakeIoModule` are unchanged
    externally but their internal calls that previously reached into `FakeFilesystem` directly now
    go through the `FakeVFS` API.
- **Add** `pyfakefs.fake_filesystem.get_active_vfs()` and the `_active_vfs` ContextVar.
- **Add** `Patcher.__enter__` / `__exit__` that set `_active_vfs` to `self.vfs._vfs`. Existing
  `setUp` / `tearDown` continue to work and also set `_active_vfs` for back-compat.
- **Fix** the lock-shaped hazards from §2 that do not require the layering: A.3, A.4, B.3, D.2, E.1,
  E.2, E.3, F.2, G.2, G.3.
- **Address** the `_with_lock` blind spot for property setters (G.2) by adding explicit `with
  self._lock:` to the four affected setters (`cwd`, `os`, `is_windows_fs`, `is_macos`).
- **Add** Cluster E and Cluster G hazards to `test_thread_safety_static.py` as new pattern
  categories.
- **Document** the contract for `_side_effect` callbacks (B.1) and
  `FakeDirectoryFromRealDirectory.entries` lazy-load (B.2): "must not acquire any lock; will run
  while the filesystem lock is held." These move into `FakeVFS` in Phase B; in Phase A they are
    documented but not yet refactored.

What ships: same public API as the retrofit, plus the lock-shaped fixes, plus a `get_active_vfs()`
opt-in. Internally, the library is now layered. Zero breaking changes. The internal refactor is a
prerequisite for Phases B and C.

What this does NOT fix: the API-shape problem from §3.4 (live references still escape). Cluster A.1,
A.2, C.1, C.2, D.1 (require the public-API surfacing in Phase B). F.1 (`tempfile.tempdir` is
process-global; deferred to Phase B where the `FakeVFS` owns the tempdir value).

### Phase B: surface FakeVFS publicly and deprecate the leaky API — minor version bump

Goal: make the layered API the recommended path, deprecate the reference-returning surface.

- **Promote** `FakeVFS` to the public API. The `Patcher` returns a `FakeVFS` from `__enter__` (the
  existing `FakeFilesystem` is still returned for back-compat via a `FakeFilesystem.vfs` property
  and via the `fs` fixture's continued return type). Documentation rewrites examples to use
  `vfs.open`, `vfs.stat`, `vfs.edit`, etc.
- **Deprecate** `FakeFilesystem.get_object`, `lresolve`, `resolve`, `root_dir`, `directory.entries`,
  and the live-reference return surface. Each emits `DeprecationWarning` on access. The warning
  message names the migration target (`vfs.get_attrs`, `vfs.edit`, `vfs.lookup`).
- **Deprecate** `FakeFile` and `FakeDirectory` as public types. Imports from
  `pyfakefs.fake_filesystem` continue to work but emit `DeprecationWarning`. Callers who need to
  inspect filesystem state use `vfs.get_attrs(path)` (returns the `FakeFileAttrs` value).
- **Move** `_side_effect` invocation out of the lock per §5.5. Lazy real-directory loading becomes
  eager.
- **Migrate** `FakePath.filesystem` (Cluster C) to read from the active VFS via `get_active_vfs()`.
  The class attribute is kept as a deprecated shim with `DeprecationWarning` on read.
- **Migrate** `FakeShutilModule` (D.1) to read its module-level state from the active VFS,
  eliminating the class-attribute lock confusion.
- **Deprecate** `Patcher().setUp()` / `tearDown()` outside a context manager. `with Patcher() as
  vfs:` is the only non-deprecated form.

What ships: the layered API is documented and recommended; the reference-returning surface is
deprecated. Test code that follows the patched-module API (`os.open`, `pathlib.Path`, `open()`,
`fs.create_file`) is unaffected. Test code that reaches behind the patched modules for
`fs.get_object` etc. emits warnings.

### Phase C: remove deprecated APIs — major version bump

Goal: complete the encapsulation.

- **Remove** `FakeFilesystem.get_object`, `lresolve`, `resolve`, `root_dir`, `directory.entries`,
  and all other reference-returning public methods. Callers use the `FakeVFS` API.
- **Remove** `FakeFile` and `FakeDirectory` from the public API. The types remain in
  `pyfakefs._inode` but are not exported from `pyfakefs.fake_filesystem`.
- **Remove** the `inspect.getmembers` auto-wrapping loop. Locking is inside `FakeVFS` per §5.2; the
  static check enforces the discipline.
- **Remove** the singleton `PATCHER`/`REF_COUNT` machinery for fake filesystems (the module-patches
  refcount via `_module_lock` stays).
- **Remove** `FakePath.filesystem` class attribute.
- **Remove** `Patcher().setUp()` / `tearDown()` direct invocation.

What ships: the redesigned library. The patched-module surface (`os.open`, `pathlib.Path`, `open()`)
is unchanged. The `FakeVFS` API is the only way to reach filesystem state outside the patched
modules.  Internal types (`FakeFile`, `FakeDirectory`, `FakeInodeTable`) are private. The library is
small, fast, and correctly layered.

### Migration tooling

The migration is bounded by the public API surface that changes:

- `pyfakefs-migrate-to-context` (Phase A→B): rewrites `Patcher().setUp()` / `tearDown()` to `with
  Patcher() as vfs:`.  Mechanical AST transform.
- `pyfakefs-migrate-to-vfs` (Phase B→C): rewrites `fs.get_object(p).attr` reads to
  `vfs.get_attrs(p).attr`, `fs.get_object(p).attr = v` writes to `with vfs.edit(p) as e: e.attr =
  v`, and `fs.root_dir` accesses to the equivalent `vfs.lookup('/')` form.  Intent-sensitive (read
  vs. write); the codemod handles the unambiguous cases and emits TODO comments for the rest.

We have written codemods of this shape for internal Cloudflare libraries; the cost is manageable. We
are willing to author and maintain these alongside the upstream changes.

### Phase ordering rationale

**Phase A is the load-bearing phase.** It builds the layered architecture as internal refactor
without changing the public API.  Phase A is shippable on its own — the lock-shaped fixes alone
materially improve the library — and it is a prerequisite for B and C. A maintainer who is
unconvinced of the §5 design but agrees with the §2 hazard fixes can ship Phase A and stop. The
internal layering adds maintenance value (tested invariants between layers) even if the public
surface never changes.

**Phase B is the user-visible contract change.** Deprecation warnings appear; recommended patterns
shift. Downstream tests need to update imports and method calls; the codemod handles the bulk. Phase
B is appropriate ~2-3 months after Phase A bakes.

**Phase C is the breaking change.** The reference-returning surface is removed. Phase C is
appropriate 12-18 months after Phase B to give downstream users time to migrate. Phase C completes
the layering.

**The phases compose, not branch.** A maintainer who ships Phase A but later decides not to do B/C
still gets a layered internal architecture and the lock-shaped fixes. The library does not become
worse from stopping early; it just doesn't become as good as it could.

---

## §7. Open questions

These are genuinely unresolved. We surface them rather than silently choose, because the decisions
affect the migration shape.

1. **Is per-thread/per-context isolation a goal we can commit to?** §5.2 assumes yes. If the answer
is no — if the maintainer wants pyfakefs to remain explicitly process-wide — then Cluster C, D, F
hazards are accept-and-document, the §5.2 redesign does not happen, and the migration shrinks to
Phase A only.

2. **Pickling `FakeFilesystem`.** The current `__getstate__` documents "caller is responsible." In a
per-context world, what does pickle/unpickle mean? Does the unpickled filesystem reactivate as the
active one? We do not have a clean answer.

3. **`os.fork()` while a `Patcher` is active.** Locks are copied in a held state; `threading.local`
instances reset in the child. The current code does not handle this; we have not seen anyone report
it as a bug. Likely accept-and-document.

4. **Subclassability of `FakeFilesystem`.** Some downstream users subclass `FakeFilesystem` to
override behaviour. The `@thread_safe_public_method` decorator pattern requires overrides to
re-decorate. Mitigation: a metaclass, or a documented "if you override a thread-safe method, you
must call `super()` or re-decorate" rule.

5. **The `FakeFileWrapper` inheritance from `io.Base` (Phase 6.2.0).** The retrofit kept this
inheritance. The Cluster E hazards intersect the IO-protocol contract in ways we have not fully
traced. A separate audit pass focused on `io.TextIOBase` / `io.BufferedIOBase` semantics under
concurrent access would be useful but is out of scope here.

6. **Maintenance cost of the static check.** Every new `_lock` pattern added to
`test_thread_safety_static.py` is a regex; regex audits decay as the code evolves. Eventually we
will want the check to use AST analysis (libcst, ast module) rather than regex. We have not built
that.

---

## Appendix A: Re-audit methodology

The 19 new hazards in §2 came from a fresh re-audit of the post-retrofit code on `fix-thread-races`
(HEAD `cae0f01` at audit time). The audit proceeded by hazard category rather than file traversal:

1. **Lock-ordering hazards** — read all sites that acquire `_class_lock` or `_lock`, traced the call
graphs.
2. **Iterator/view escapes beyond Shape 5** — grepped `return self\._[a-z_]+` and similar for
live-mutable-view returns.
3. **Callback re-entry** — read all `__del__` methods, all `weakref` callbacks, all places
`self.<callable>()` is called inside a locked region.
4. **Patcher singleton lifecycle** — read `__new__`, `__init__`, `setUp`, `tearDown`,
`start_patching`, `stop_patching` line by line.
5. **`inspect.getmembers` blind spots** — enumerated public methods and property setters; the
property-setter gap was the headline finding.
6. **Fake module re-entry** — traced what threads spawned by user code see when accessing `os.*`;
the `_id_thread_state` and `use_original` story is documented but incomplete.
7. **Process globals** — grepped `tempfile\.` and `sys\.meta_path` for mutation sites.

The audit took roughly 60 minutes of subagent time against the post- retrofit code. It found 19 new
hazards (12 high-confidence, 6 medium, 1 speculation). The audit was not exhaustive — categories not
investigated include `os.fork()` semantics in detail, full pickle/copy tracing, and the `io.Base`
inheritance interaction with concurrent IO. Those are flagged as open questions in §7.

The audit's output is reproduced verbatim in the audit subagent's session log; the §2 cluster
organisation is this report's contribution.

---

## Appendix B: Reproducer sketches

For each high-confidence hazard, a reproducer sketch that triggers the failure. These are not full
MREs; the existing `pyfakefs/tests/threading_mres/` directory has the template for shapes 1–7.
Authoring full MREs for the 12 high-confidence findings is a Phase A deliverable.

(Reproducer sketches would extend this appendix by 4-6 pages; we omit them from this draft and
provide them on request or in the issue thread when this report goes public.)
