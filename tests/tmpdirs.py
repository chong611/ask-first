"""Temp folders for tests, removed when the run exits, so runs do not pile up folders in the temp dir.
TemporaryDirectory also removes read-only files (git objects on Windows), which a plain rmtree leaves."""
import atexit
import tempfile


def mkdtemp() -> str:
    folder = tempfile.TemporaryDirectory(prefix="pb-test-", ignore_cleanup_errors=True)
    atexit.register(folder.cleanup)  # also keeps it alive until then
    return folder.name
