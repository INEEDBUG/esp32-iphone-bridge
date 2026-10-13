"""Cross-instance and cross-process tests for cooperative stop generations."""
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from phonelab.stop_signal import StopSignalError, StopStore


def test_token_observes_stop_from_another_store_and_new_capture_is_fresh(tmp_path):
    first = StopStore(tmp_path)
    old = first.capture()
    second = StopStore(tmp_path)
    result = second.publish_stop()
    assert result["state"] == "stopped" and result["generation"] == 1
    assert old.is_set()
    new = first.capture()
    assert not new.is_set()
    new.set()
    assert new.is_set()
    assert old.is_set()  # cancellation remains latched


def _publish_worker(root, count, ready, go):
    store = StopStore(Path(root))
    ready.put(True)
    go.wait(3)
    for _ in range(count):
        store.publish_stop()


def test_concurrent_process_publishers_serialize_generation_increments(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    go = ctx.Event()
    store = StopStore(tmp_path)
    assert store.capture()
    workers = [ctx.Process(target=_publish_worker, args=(str(tmp_path), 8, ready, go))
               for _ in range(3)]
    for worker in workers:
        worker.start()
    for _ in workers:
        assert ready.get(timeout=5)
    go.set()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert store.capture().generation == 24


@pytest.mark.parametrize("mutation", ["missing", "malformed", "oversize"])
def test_corrupt_or_disappeared_record_cancels_existing_token_and_never_reinitializes(tmp_path, mutation):
    store = StopStore(tmp_path)
    token = store.capture()
    record = tmp_path / "stop-signal.json"
    if mutation == "missing":
        record.unlink()
    elif mutation == "malformed":
        record.write_text("{broken", encoding="ascii")
    else:
        record.write_bytes(b"x" * 1025)
    assert token.is_set()
    with pytest.raises(StopSignalError):
        store.capture()


def test_token_latches_cancel_after_transient_read_error(monkeypatch, tmp_path):
    store = StopStore(tmp_path)
    token = store.capture()
    original = store._read_record
    monkeypatch.setattr(store, "_read_record", lambda: (_ for _ in ()).throw(OSError()))
    assert token.is_set()
    monkeypatch.setattr(store, "_read_record", original)
    assert token.is_set()


def test_lock_contention_fails_closed_within_half_second(tmp_path):
    store = StopStore(tmp_path)
    store.capture()
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with store._locked(timeout=0.5):
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(1)
    started = time.monotonic()
    with pytest.raises(StopSignalError):
        store.publish_stop()
    assert time.monotonic() - started <= 0.75
    release.set()
    thread.join(1)


def test_token_lock_contention_latches_quickly_for_serial_poll(tmp_path):
    store = StopStore(tmp_path)
    token = store.capture()
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with store._locked(timeout=0.5):
            held.set()
            release.wait(2)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert held.wait(1)
    started = time.monotonic()
    assert token.is_set()
    assert time.monotonic() - started <= 0.15
    release.set()
    thread.join(1)
    assert token.is_set()


def test_generation_advances_without_changing_epoch(tmp_path):
    store = StopStore(tmp_path)
    token = store.capture()
    original_epoch = token.epoch
    store.publish_stop()
    next_token = store.capture()
    assert next_token.generation == token.generation + 1
    assert next_token.epoch == original_epoch
