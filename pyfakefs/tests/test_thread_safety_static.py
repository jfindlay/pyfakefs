"""Static-pattern check for thread-safety hazards in pyfakefs source.

This test greps the source for unsynchronized read-modify-write of named
hazardous attributes.  It does not require any locking or concurrent execution:
it tests a syntactic property of the source, so it does not decay silently
across revisions the way runtime concurrency tests do.

Shape 1 (THREAD-SAFETY-AUDIT.md) documents the hazard: `x += 1` on a
shared mutable attribute is a non-atomic read-modify-write under free-threaded
CPython (PEP 703) and a multi-step race on all interpreters.

Adding a new unsynchronized RMW of any attribute in `RMW_ATTRS` (or a write
to any attribute in `WRITE_ATTRS`) without:
  a) a surrounding lock context, AND
  b) a `# thread_safe_ok: <reason>` comment on the same line,

will cause this test to fail.  Update `RMW_ATTRS` / `WRITE_ATTRS` as new
shapes are catalogued in THREAD-SAFETY-AUDIT.md.

Note: False positives are possible (e.g., assignments inside `__init__`
before any thread holds a reference).  Suppress known-safe sites by adding
`# thread_safe_ok: <reason>` to the line.  Do not use `# noqa` — the
opt-out comment is intentionally specific to this check so that it is
visible and searchable.

Related:
  - THREAD-SAFETY-AUDIT.md § Static-pattern check
  - UMBRELLA-ISSUE-DRAFT.md § Fix strategies § Strategy 2
  - pytest-dev/pyfakefs#1317, #1318
"""
import re
import pathlib
import unittest

# SRC resolves to the pyfakefs/ package directory (one level above tests/).
SRC = pathlib.Path(__file__).parent.parent

# Attributes confirmed as Shape 1 (integer RMW) hazards in THREAD-SAFETY-AUDIT.md.
# Extend this list as new shapes are catalogued.
#
# Two sub-categories:
#   RMW_ATTRS:    augmented assignment (+=, -=) is the hazard
#   WRITE_ATTRS:  any plain write (= ...) is the hazard (Shape 7 module globals)
RMW_ATTRS = [
    "last_ino",
    "last_dev",
    "st_nlink",
    "used_size",
]
WRITE_ATTRS = [
    "USER_ID",
    "GROUP_ID",
]

# Matches augmented RMW:  last_ino += ...   st_nlink -= ...
_RMW_PAT = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in RMW_ATTRS) + r")\s*[+\-]="
)

# Matches any write to Shape 7 module globals:  USER_ID = ...
# Excludes == comparisons by requiring a single = not preceded or followed by =.
_WRITE_PAT = re.compile(
    r"\b(" + "|".join(re.escape(a) for a in WRITE_ATTRS) + r")\s*(?<![=!<>])=(?!=)"
)


class StaticThreadSafetyTest(unittest.TestCase):
    """Static source-scan checks for thread-safety hazards."""

    def test_no_unsynchronized_rmw(self) -> None:
        """Catch sibling hazards of the umbrella's Shape 1 (int RMW) by attribute name.

        Scans all `pyfakefs/*.py` source files (excluding `tests/`).  Reports
        every line that matches a write to a hazardous attribute without a
        `# thread_safe_ok` opt-out comment.

        To suppress a known-safe site (e.g., inside `__init__` before any thread
        holds a reference), add `# thread_safe_ok: <reason>` to that line.
        """
        bad: list[str] = []
        # Scan *.py directly in the package directory; the tests/ subdirectory is
        # excluded by the glob pattern (it does not match *.py at this level).
        for py in SRC.glob("*.py"):
            for lineno, line in enumerate(py.read_text().splitlines(), 1):
                if (
                    (_RMW_PAT.search(line) or _WRITE_PAT.search(line))
                    and "thread_safe_ok" not in line
                ):
                    bad.append(f"{py.name}:{lineno}: {line.strip()}")

        self.assertFalse(
            bad,
            "Unsynchronized RMW of hazardous attributes detected.\n"
            "See THREAD-SAFETY-AUDIT.md § Shape 1 for the hazard description.\n"
            "If this site is safe (e.g., init-time only), add:\n"
            "    # thread_safe_ok: <reason>\n"
            "to the offending line.  Do not suppress without documentation.\n\n"
            + "\n".join(bad),
        )
