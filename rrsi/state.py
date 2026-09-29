# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Durable run state: atomic file replacement and the per-domain writer lock.

A reader of runs/<domain>/ sees either the old or the new version of a state
file, never a torn one; one mutating command per domain holds runs/<domain>/.lock
at a time, so two writers cannot interleave history, branch and frontier updates.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


def atomic_write_text(path: Path | str, text: str) -> None:
    """Temp file in the same directory, flush + fsync, then os.replace."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_json(path: Path | str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1))


class RunLock:
    """Exclusive, non-blocking writer lock (fcntl.flock, POSIX) on `path`, normally
    runs/<domain>/.lock. The holder's pid is written into the file; contention is a
    SystemExit naming the domain and that pid. The kernel releases the lock when the
    holder exits, however it exits."""

    def __init__(self, path: Path | str, domain: str | None = None):
        self.path = Path(path)
        self.domain = domain or self.path.parent.name
        self.fd: int | None = None

    def __enter__(self) -> "RunLock":
        import fcntl                     # POSIX only; imported here so the rest loads anywhere
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 32).decode(errors="replace").strip() or "unknown"
            os.close(fd)
            raise SystemExit(f"domain {self.domain}: another rrsi writer holds {self.path} "
                             f"(pid {holder}); wait for it to finish or stop it") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return self

    def __exit__(self, *exc) -> None:
        import fcntl
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
