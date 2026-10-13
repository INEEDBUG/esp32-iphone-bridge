"""Fail-closed, cross-process cooperative stop generations for scoped input.

The store never opens a device. Callers capture a generation for one explicit
run; publishing a stop invalidates existing tokens without permanently blocking
later explicit runs.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


class StopSignalError(RuntimeError):
    """The private stop-generation store could not be read or safely changed."""


class StopToken:
    """Event-shaped token that latches cancellation on mismatch or any read error."""
    def __init__(self, store: "StopStore", epoch: str, generation: int):
        self._store = store
        self.epoch = epoch
        self.generation = generation
        self._cancelled = threading.Event()

    def set(self):
        self._cancelled.set()

    def is_set(self):
        if self._cancelled.is_set():
            return True
        try:
            current = self._store._read_record(timeout=0.025)
            if current["epoch"] != self.epoch or current["generation"] != self.generation:
                self._cancelled.set()
        except Exception:
            self._cancelled.set()
        return self._cancelled.is_set()


class _HeldLock:
    def __init__(self, handle, never_initialized):
        self.handle = handle
        self.never_initialized = never_initialized

    def mark_initialized(self):
        self.handle.seek(1)
        self.handle.write(b"I")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.never_initialized = False


class StopStore:
    VERSION = 1
    MAX_RECORD_BYTES = 1024
    LOCK_TIMEOUT_SECONDS = 0.5

    def __init__(self, private: Path):
        self.private = Path(private)
        self.record_path = self.private / "stop-signal.json"
        self.lock_path = self.private / "stop-signal.lock"

    @contextmanager
    def _locked(self, timeout=LOCK_TIMEOUT_SECONDS):
        """Yield whether this call created the persistent first-use sentinel."""
        self.private.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            fd = os.open(self.lock_path, os.O_RDWR)
        handle = os.fdopen(fd, "r+b", buffering=0)
        acquired = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + min(float(timeout), self.LOCK_TIMEOUT_SECONDS)
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise StopSignalError("stop signal lock unavailable") from None
                    time.sleep(0.01)
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                handle.seek(0)
                handle.write(b"\0N")
            elif size == 1:
                handle.seek(1)
                handle.write(b"N")
            elif size != 2:
                raise StopSignalError("stop signal lock unavailable")
            handle.flush()
            handle.seek(0)
            if handle.read(1) != b"\0":
                raise StopSignalError("stop signal lock unavailable")
            marker = handle.read(1)
            if marker not in (b"N", b"I"):
                raise StopSignalError("stop signal lock unavailable")
            yield _HeldLock(handle, marker == b"N")
        except StopSignalError:
            raise
        except Exception:
            raise StopSignalError("stop signal lock unavailable") from None
        finally:
            if acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()

    def _read_unlocked(self):
        try:
            size = self.record_path.stat().st_size
            if size <= 0 or size > self.MAX_RECORD_BYTES:
                raise ValueError
            raw = self.record_path.read_bytes()
            if len(raw) != size or len(raw) > self.MAX_RECORD_BYTES:
                raise ValueError
            record = json.loads(raw.decode("ascii"))
            if not isinstance(record, dict) or set(record) != {"version", "epoch", "generation"}:
                raise ValueError
            if record["version"] != self.VERSION or type(record["version"]) is not int:
                raise ValueError
            epoch = record["epoch"]
            if not isinstance(epoch, str) or str(uuid.UUID(epoch)) != epoch:
                raise ValueError
            generation = record["generation"]
            if type(generation) is not int or generation < 0:
                raise ValueError
            return {"version": self.VERSION, "epoch": epoch, "generation": generation}
        except Exception:
            raise StopSignalError("stop signal record unavailable") from None

    def _read_record(self, timeout=LOCK_TIMEOUT_SECONDS):
        with self._locked(timeout=timeout):
            return self._read_unlocked()

    def _write_unlocked(self, record):
        payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(payload) > self.MAX_RECORD_BYTES:
            raise StopSignalError("stop signal record unavailable")
        fd, temp_name = tempfile.mkstemp(prefix=".stop-signal-", suffix=".tmp", dir=self.private)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.record_path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise StopSignalError("stop signal record unavailable") from None

    def _first_record(self, held_lock):
        if not held_lock.never_initialized:
            raise StopSignalError("stop signal record unavailable")
        record = {"version": self.VERSION, "epoch": str(uuid.uuid4()), "generation": 0}
        self._write_unlocked(record)
        held_lock.mark_initialized()
        return record

    def capture(self) -> StopToken:
        with self._locked() as held_lock:
            if self.record_path.exists():
                if held_lock.never_initialized:
                    held_lock.mark_initialized()
                record = self._read_unlocked()
            else:
                record = self._first_record(held_lock)
        return StopToken(self, record["epoch"], record["generation"])

    def publish_stop(self) -> dict:
        with self._locked() as held_lock:
            if self.record_path.exists():
                if held_lock.never_initialized:
                    held_lock.mark_initialized()
                record = self._read_unlocked()
            else:
                record = self._first_record(held_lock)
            updated = {"version": self.VERSION, "epoch": record["epoch"],
                       "generation": record["generation"] + 1}
            self._write_unlocked(updated)
        return {"state": "stopped", "generation": updated["generation"]}
