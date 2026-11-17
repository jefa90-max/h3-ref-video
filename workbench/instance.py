"""Keep one workbench service per repository, even when ports differ."""
from contextlib import contextmanager
import os


@contextmanager
def single_instance(storage):
    storage.mkdir(parents=True, exist_ok=True)
    with (storage / "server.lock").open("a+b") as lock:
        locked_nt = False
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                locked_nt = True
            else:
                import fcntl
                lock.seek(0)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            from .i18n import message
            raise SystemExit(message("instance")) from exc
        try:
            yield
        finally:
            if locked_nt:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
