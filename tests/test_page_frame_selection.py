from contextlib import nullcontext
from types import SimpleNamespace
import threading

import pytest

from phonelab.page_scope import PageMovementExperiment, PageScopeError
from phonelab.visual_marker import MarkerError, VisualMarkerTracker, _nonce_hash
from test_visual_marker import fixture


class Clock:
    def __init__(self):
        self.now = 10.0
        self.sleeps = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


def frame(frame_sequence, clock, *, counter=None, nonce="hello", received=None, **metadata):
    png, _ = fixture(frame_sequence if counter is None else counter, nonce_hash=_nonce_hash(nonce))
    values = {
        "target": "test-target",
        "source": "USB_SCREENSHOT",
        "orientation": "portrait",
        "fresh": True,
        "state": "fresh",
        "locked": "UNKNOWN",
        "width": 828,
        "height": 1792,
        "sequence": frame_sequence,
        "received_monotonic": clock() if received is None else received,
    }
    values.update(metadata)
    return SimpleNamespace(image_bytes=png, metadata=values)


class Observer:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]

    def events(self, run_id):
        return {"run": {"run_id": run_id, "nonce": "hello", "test_id": "ble-round-01"},
                "events": []}


def make_action(clock, observer, *, cancel=None, backend_opener=None):
    token = cancel or threading.Event()
    opened = []
    if backend_opener is None:
        backend_opener = lambda: opened.append(True)
    action = PageMovementExperiment(
        observer, target="test-target", run_id="a" * 16, nonce="hello",
        test_id="ble-round-01", backend_opener=backend_opener,
        lease_factory=nullcontext, cancel_token=token, clock=clock, sleep=clock.sleep)
    return action, token, opened


def test_same_cached_sequence_waits_once_then_selects_the_next_frame():
    clock = Clock()
    observer = Observer([frame(4, clock), frame(5, clock, received=10.05)])
    action, _, _ = make_action(clock, observer)

    selected, state = action._observe(VisualMarkerTracker("hello"), 4)

    assert selected.metadata["sequence"] == 5
    assert state["counter"] == 5
    assert clock.sleeps == [0.05]
    assert observer.calls == 2


def test_forever_cached_sequence_fails_within_existing_selection_deadline():
    clock = Clock()
    cached = frame(4, clock)
    observer = Observer([cached])
    action, _, _ = make_action(clock, observer)

    with pytest.raises(PageScopeError, match="new image for bounded processing unavailable"):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert 1.5 <= clock.now - 10.0 <= 1.6
    assert observer.calls == len(clock.sleeps) + 1
    assert all(delay == 0.05 for delay in clock.sleeps)


def test_next_frame_arriving_after_selection_deadline_is_not_selected():
    clock = Clock()
    cached = frame(4, clock)
    arrived_late = frame(5, clock, received=11.5)

    class ObserverAfterDeadline(Observer):
        def snapshot(self):
            self.calls += 1
            return arrived_late if clock() >= 11.5 else cached

    observer = ObserverAfterDeadline([])
    action, _, _ = make_action(clock, observer)

    with pytest.raises(PageScopeError, match="new image for bounded processing unavailable"):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert clock.now >= 11.5
    assert observer.calls == len(clock.sleeps) + 1


def test_action_does_not_open_backend_when_frame_stays_cached():
    clock = Clock()
    first = frame(1, clock)
    observer = Observer([first])
    action, _, opened = make_action(clock, observer)

    with pytest.raises(PageScopeError):
        action.move(1, 0)

    assert not opened
    assert observer.calls > 1


def test_cancellation_during_wait_stops_before_another_snapshot_read():
    clock = Clock()
    token = threading.Event()
    cached = frame(4, clock)
    observer = Observer([cached])
    action, _, _ = make_action(clock, observer, cancel=token)
    clock.on_sleep = token.set

    with pytest.raises(PageScopeError, match="generation stopped"):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert clock.sleeps == [0.05]
    assert observer.calls == 1


def test_lower_sequence_is_a_hard_regression_without_waiting():
    clock = Clock()
    observer = Observer([frame(3, clock)])
    action, _, _ = make_action(clock, observer)

    with pytest.raises(PageScopeError):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert clock.sleeps == []
    assert observer.calls == 1


@pytest.mark.parametrize("fault,metadata", [
    ("target", {"target": "other-target"}),
    ("source", {"source": "CLIENT_CAPTURE"}),
    ("orientation", {"orientation": "landscape"}),
    ("lock", {"locked": "UNLOCKED"}),
    ("fresh", {"fresh": False}),
    ("state", {"state": "stale"}),
    ("geometry", {"width": 827}),
    ("sequence_bool", {"sequence": True}),
    ("sequence_float", {"sequence": 4.0}),
    ("receipt_bool", {"received_monotonic": True}),
])
def test_invalid_page_scope_metadata_fails_immediately_even_on_cached_sequence(fault, metadata):
    clock = Clock()
    observer = Observer([frame(4, clock, **metadata)])
    action, _, _ = make_action(clock, observer)

    with pytest.raises(PageScopeError):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert clock.sleeps == []
    assert observer.calls == 1


@pytest.mark.parametrize("received", [7.9, 12.1])
def test_receipt_older_than_two_seconds_or_in_the_future_fails_without_wait(received):
    clock = Clock()
    observer = Observer([frame(4, clock, received=received)])
    action, _, _ = make_action(clock, observer)

    with pytest.raises(PageScopeError):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert clock.sleeps == []
    assert observer.calls == 1


def test_changed_page_marker_on_next_sequence_is_rejected_after_wait():
    clock = Clock()
    observer = Observer([frame(4, clock), frame(5, clock, nonce="changed")])
    action, _, _ = make_action(clock, observer)

    with pytest.raises(MarkerError):
        action._observe(VisualMarkerTracker("hello"), 4)

    assert clock.sleeps == [0.05]
    assert observer.calls == 2
