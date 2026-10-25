"""Return freed heap memory to the OS after large one-off jobs (baseline rebuild, backfill).

CPython and glibc keep freed memory mapped for reuse, so a short spike can leave the
process resident at its peak for hours. Railway bills resident memory, so after big
jobs we collect garbage and ask glibc to trim. Elsewhere (Windows, musl) only the
garbage collection runs.
"""

import ctypes
import gc

_malloc_trim = None
_malloc_trim_checked = False


def _get_malloc_trim():
    global _malloc_trim, _malloc_trim_checked
    if not _malloc_trim_checked:
        _malloc_trim_checked = True
        try:
            _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
            _malloc_trim.argtypes = [ctypes.c_size_t]
            _malloc_trim.restype = ctypes.c_int
        except (OSError, AttributeError):
            _malloc_trim = None
    return _malloc_trim


def release_freed_memory():
    """Blocking (tens of ms); call via asyncio.to_thread. Returns True if glibc trimmed."""
    gc.collect()
    malloc_trim = _get_malloc_trim()
    if malloc_trim is None:
        return False
    return bool(malloc_trim(0))
