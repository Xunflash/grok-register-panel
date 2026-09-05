"""Producer-owned pipe rotation; never truncate a child's open output file."""
import os
from pathlib import Path
import threading
import time


class RotatingPipe:
    def __init__(self, path, max_bytes=8 * 1024 * 1024, backups=3):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.reader, self.writer = os.pipe()
        self.error = None
        self.thread = threading.Thread(target=self._copy, name="rotating-log", daemon=True)
        self.thread.start()

    def fileno(self):
        return self.writer

    def write(self, value):
        data = value.encode() if isinstance(value, str) else value
        while data:
            data = data[os.write(self.writer, data):]

    def flush(self):
        return None

    def close(self):
        if self.writer is not None:
            os.close(self.writer)
            self.writer = None

    def _open(self):
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        return os.fdopen(descriptor, "ab", buffering=0)

    def _rotate(self):
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else self.path.with_name(self.path.name + f".{index - 1}")
            target = self.path.with_name(self.path.name + f".{index}")
            if source.exists():
                os.replace(source, target)

    def _copy(self):
        destination = None
        try:
            destination = self._open()
            with os.fdopen(self.reader, "rb", buffering=0) as source:
                while chunk := source.read(min(65536, self.max_bytes)):
                    if destination.tell() + len(chunk) > self.max_bytes:
                        destination.close()
                        self._rotate()
                        destination = self._open()
                    destination.write(chunk)
        except OSError as error:
            self.error = error
        finally:
            if destination is not None:
                destination.close()


def prune_closed_logs(directory, protected=(), max_bytes=256 * 1024 * 1024, max_age=7 * 86400, now=None):
    now = time.time() if now is None else now
    protected = {Path(path).resolve() for path in protected}
    candidates = []
    for path in Path(directory).iterdir():
        if path.is_symlink() or not path.is_file() or not (path.name.endswith(".log") or ".log." in path.name):
            continue
        info = path.stat()
        candidates.append((info.st_mtime, info.st_size, path))
    total = sum(size for _, size, _ in candidates)
    removed = 0
    for modified, size, path in sorted(candidates):
        if path.resolve() in protected:
            continue
        if now - modified > max_age or total > max_bytes:
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
    return {"removed": removed, "bytes": total}
