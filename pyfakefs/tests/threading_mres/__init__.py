# Minimal Reproducible Examples for pyfakefs thread-safety hazards.
#
# Each MRE is directly executable:
#
#     python  pyfakefs/tests/threading_mres/mre_shape1_last_ino.py
#     pypy3   pyfakefs/tests/threading_mres/mre_shape1_last_ino.py
#     python3.13t pyfakefs/tests/threading_mres/mre_shape1_last_ino.py  # GIL-less
#
# Pre-fix: fires on PyPy or CPython 3.13 free-threaded (--disable-gil).
# Post-fix: all MREs exit 0 (the coarse FakeFilesystem RLock, thread-local
# uid/gid, and Patcher._class_lock serialise the racing operations).
#
# The unit tests in pyfakefs/tests/test_threading.py cover the same
# invariants deterministically; these MREs serve as historical evidence and
# as standalone reproduction tools for interpreter-specific regression checks.
