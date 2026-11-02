"""Keep one workbench service per repository, even when ports differ."""
from contextlib import contextmanager
import os


@contextmanager
def single_instance(storage):
    storage.mkdir(parents=True, exist_ok=True)
    with (storage / "server.lock").open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            acquire = lambda: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            acquire()
        except OSError as exc:
            from .i18n import message
            raise SystemExit(message("instance")) from exc
        yield
