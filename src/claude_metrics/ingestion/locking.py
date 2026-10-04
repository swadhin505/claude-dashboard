"""One OS-managed writer lock shared by every ingestion entrypoint."""

import os
from contextlib import contextmanager
from pathlib import Path


class ScanBusy(OSError):
    pass


@contextmanager
def writer_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    # Keep the inode: unlinking a lock file can permit two simultaneous owners.
    with (data_dir / "ingestion.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ScanBusy("another scan is active; retry when it finishes") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ScanBusy("another scan is active; retry when it finishes") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
