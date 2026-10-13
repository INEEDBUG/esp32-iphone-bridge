import time
from pathlib import Path

import pytest

from phonelab.control import Frame
from phonelab.operator import CaptureProof, OperatorError, OperatorSession, TRUSTED_CAPTURE_SOURCE


class Backend:
    name = "injected-test-backend"
    def __init__(self): self.calls = []; self.closed = 0; self.result = {"id": "unused", "state": "executed", "verified": False}
    def execute(self, request):
        self.calls.append(request)
        return {**self.result, "id": request["id"]}
    def close(self): self.closed += 1


class Provider:
    def __init__(self, root, target="target-1", lock_state="UNLOCKED"):
        self.path = root / "frame.jpg"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"private capture placeholder")
        self.target = target
        self.lock_state = lock_state
        self.sequence = 0
        self.generation = "usb-epoch-1"
        self.width, self.height = 390, 844
        self.frame_target = "untrusted-frame-label"

    def latest_frame(self):
        self.sequence += 1
        frame = Frame(self.frame_target, self.sequence, time.monotonic(), self.width,
                      self.height, "portrait", locked=False)
        return frame, self.path

    def verify_current(self, frame, path):
        return CaptureProof(self.target, frame.sequence, True, self.lock_state,
                            self.generation, time.monotonic(), TRUSTED_CAPTURE_SOURCE)


def make_session(tmp_path, *, ready=True, provider=None):
    private_images = tmp_path / "private" / "airplay-phone-frames"
    provider = provider or Provider(private_images)
    backend = Backend()
    opened = []
    session = OperatorSession("target-1", private_capture_root=private_images,
        capture_provider=provider, readiness_fn=lambda **_: {"ready": ready},
        backend_opener=lambda **_: opened.append(True) or backend)
    return session, backend, opened, provider


def test_default_has_no_backend_and_never_opens_during_status_or_screen(tmp_path):
    provider = Provider(tmp_path / "private" / "airplay-phone-frames")
    session, backend, opened, _ = make_session(tmp_path, provider=provider)
    assert session.get_status()["ACTIVE_INPUT_BACKEND"] == "none"
    screen = session.get_screen()
    assert screen["frame"].locked is False
    assert screen["frame"].target == "target-1"  # independently bound proof, not raw frame label
    assert session.get_status()["raw_relative_move"] is False
    assert opened == [] and backend.closed == 0


def test_backend_selection_requires_runtime_readiness(tmp_path):
    session, backend, opened, _ = make_session(tmp_path, ready=False)
    with pytest.raises(OperatorError, match="readiness"):
        session.select_backend()
    assert opened == [] and backend.closed == 0


@pytest.mark.parametrize("lock_state", ["UNKNOWN", "LOCKED"])
def test_untrusted_or_locked_capture_never_arms_and_closes_owned_backend(tmp_path, lock_state):
    provider = Provider(tmp_path / "private" / "airplay-phone-frames", lock_state=lock_state)
    session, backend, opened, _ = make_session(tmp_path, provider=provider)
    session.select_backend()
    session.acquire("operator")
    with pytest.raises(OperatorError, match="capture failed verification"):
        session.get_screen()
    assert opened == [True] and backend.closed == 1
    assert session.get_status()["armed"] is False
    assert session.get_status()["ACTIVE_INPUT_BACKEND"] == "none"


def test_raw_frame_flags_and_target_do_not_substitute_for_current_device_proof(tmp_path):
    class NoProofProvider(Provider):
        verify_current = None
    provider = NoProofProvider(tmp_path / "private" / "airplay-phone-frames")
    session, backend, _, _ = make_session(tmp_path, provider=provider)
    session.select_backend()
    with pytest.raises(OperatorError, match="trusted current-device capture"):
        session.get_screen()
    assert backend.closed == 1


def test_geometry_gate_exposes_relative_input_only_and_rejects_taps(tmp_path):
    session, backend, _, _ = make_session(tmp_path)
    session.select_backend()
    session.acquire("operator")
    result = session.calibrate()
    assert result == {"armed": True, "calibration_kind": "geometry_gate_only",
        "relative_move": True, "relative_vertical_scroll": True, "tap_supported": False,
        "swipe_supported": False, "absolute_pointer_calibration": False}
    assert session.get_status()["raw_relative_move"] is True
    with pytest.raises(OperatorError, match="taps/swipes"):
        session.submit("click", {})
    assert backend.calls == []
    assert session.submit("move", {"dx": 2, "dy": -1})["state"] == "executed"
    assert len(backend.calls) == 1


def test_scroll_enforces_vertical_only(tmp_path):
    session, backend, _, _ = make_session(tmp_path)
    session.select_backend()
    session.acquire("operator")
    session.calibrate()
    with pytest.raises(OperatorError, match="vertical"):
        session.submit("scroll", {"v": 1, "h": 1})
    assert backend.calls == []
    assert session.submit("scroll", {"v": 1, "h": 0})["state"] == "executed"
    assert len(backend.calls) == 1


def test_unknown_ack_closes_owned_backend_and_rotates_session_without_replay(tmp_path):
    session, backend, opened, _ = make_session(tmp_path)
    backend.result = {"state": "unknown", "verified": False}
    session.select_backend()
    session.acquire("operator")
    session.calibrate()
    old_session = session.controller.session
    result = session.submit("move", {"dx": 1, "dy": 0})
    assert result["state"] == "unknown" and "do not replay" in result["error"]
    assert len(backend.calls) == 1 and backend.closed == 1
    assert opened == [True] and session.controller.session != old_session
    assert session.get_status()["ACTIVE_INPUT_BACKEND"] == "none"


def test_watchdog_closes_stale_owned_backend_without_capture_or_reopen(tmp_path):
    session, backend, opened, provider = make_session(tmp_path)
    session.select_backend()
    session.acquire("operator")
    session.calibrate()
    sequence = provider.sequence
    old_session = session.controller.session
    session.controller.frame.received_monotonic -= 10
    result = session.watchdog()
    assert result["state"] == "stopped" and result["backend_closed"] is True
    assert backend.closed == 1 and provider.sequence == sequence
    assert opened == [True] and session.controller.session != old_session
    assert session.get_status()["ACTIVE_INPUT_BACKEND"] == "none"


@pytest.mark.parametrize("change", ["geometry", "generation"])
def test_capture_geometry_or_generation_change_stops_closes_and_invalidates(tmp_path, change):
    session, backend, _, provider = make_session(tmp_path)
    session.select_backend()
    session.acquire("operator")
    session.calibrate()
    if change == "geometry": provider.width = 844; provider.height = 390
    else: provider.generation = "usb-epoch-2"
    with pytest.raises(OperatorError, match="changed"):
        session.get_screen()
    assert backend.closed == 1
    assert session.get_status()["armed"] is False
    assert session.get_status()["owner"] is None


def test_only_session_owned_backend_is_closed(tmp_path):
    session, _, _, _ = make_session(tmp_path)
    external = Backend()
    session.controller.backend = external
    session.stop()
    assert external.closed == 0
    assert session.controller.backend is None
