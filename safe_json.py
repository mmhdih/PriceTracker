"""Locked, atomic JSON updates for daily ticks and personal finance.

Corruption is an error, never a reason to replace a database with an empty one.
"""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import tempfile
import time


@contextmanager
def file_lock(path, timeout=30):
    path = Path(str(path) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    # Kernel lock is released even if the process crashes. Lock file stays.
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Lock busy: {path}')
                time.sleep(.05)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path, default=None):
    path = Path(path)
    try:
        with path.open(encoding='utf-8') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        if default is None:
            raise
        return copy.deepcopy(default)
    if not isinstance(value, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return value


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def update_json(path, default, mutate):
    with file_lock(path):
        value = read_json(path, default)
        result = mutate(value)
        # Keep the last valid version. Never back up corrupt data as valid.
        if Path(path).exists():
            read_json(path)
            shutil.copy2(path, str(path) + '.bak')
        atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return result


def save_json(path, value):
    def replace(current):
        current.clear()
        current.update(copy.deepcopy(value))
    return update_json(path, {}, replace)
