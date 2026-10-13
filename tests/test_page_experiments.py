"""Offline tests for the explicit synthetic-page experiment entry point."""
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from phonelab.page_experiments import run_experiment
import phonelab.page_experiments as page_experiments
from test_visual_marker import fixture


TARGET = "iphone-ac39233f6b70"
RUN_ID = "0123456789abcdef"
NONCE = "hello"
TOKEN = "test-token-never-output"


class FakeObserver:
    def __init__(self):
        self.seq = 0
        self.rows = []
        self.fault = None
        self.clock = lambda: 10.

    def visible_run(self, *, test_id, seed):
        assert (test_id, seed) == ("ble-round-01", "42")
        return {"run_id": RUN_ID, "nonce": NONCE, "test_id": test_id,
                "seed": seed, "phone_verified": False}

    def snapshot(self):
        self.seq += 1
        counter = 1 if self.fault == "frozen" else self.seq
        png, _ = fixture(counter)
        meta = {"target": TARGET, "source": "USB_SCREENSHOT", "sequence": self.seq,
                "received_monotonic": self.clock(), "width": 828, "height": 1792,
                "orientation": "portrait", "locked": "UNKNOWN", "state": "fresh", "fresh": True}
        if self.fault == "wrong_target":
            meta["target"] = "iphone-000000000000"
        if self.fault == "stale":
            meta["received_monotonic"] = -10.
        return SimpleNamespace(image_bytes=png, metadata=meta, age_seconds=0.)

    def events(self, run_id):
        if self.fault == "wrong_run":
            return {"run": {"run_id": "abcdef0123456789", "nonce": NONCE,
                             "test_id": "ble-round-01", "seed": "42", "phone_verified": False}, "events": []}
        return {"run": {"run_id": RUN_ID, "nonce": NONCE,
                         "test_id": "ble-round-01", "seed": "42", "phone_verified": False}, "events": self.rows}


class FakeBackend:
    def __init__(self, result=None):
        self.calls = []
        self.closed = 0
        self.result = result or {"state": "executed", "ble_connected": True}
        self.on_execute = None

    def execute(self, request):
        self.calls.append(request)
        if self.on_execute:
            self.on_execute()
        return self.result

    def close(self):
        self.closed += 1


def setup(tmp_path, *, fault=None, backend=None):
    data = tmp_path / "task-data"
    private = tmp_path / "private"
    (private).mkdir(parents=True)
    (private / "runtime.json").write_text(json.dumps({"port": 1, "token": TOKEN}), encoding="utf-8")
    observer = FakeObserver()
    class Clock:
        now = 10.
        def __call__(self): return self.now
        def sleep(self, seconds): self.now += seconds
        def advance(self, seconds): self.now += seconds
    clock = Clock()
    observer.clock = clock
    observer.fault = fault
    backend = backend or FakeBackend()
    opens = []
    result = lambda *args, **kwargs: observer
    opener = lambda: opens.append(True) or backend
    kwargs = dict(data_root=data, private_root=private, client_factory=result,
                  target_reader=lambda _root: ("serial-never-reported", TARGET),
                  backend_opener=opener, lease_factory=lambda: nullcontext(),
                  clock=clock, sleep=clock.sleep)
    return kwargs, observer, backend, opens, private


def test_observe_is_read_only_saves_only_nonce_matched_synthetic_frame(tmp_path):
    kwargs, observer, backend, opens, private = setup(tmp_path)
    result = run_experiment("observe", **kwargs)
    assert result["state"] == "observed" and result["verified"] is False
    assert not opens and not backend.calls
    evidence = private / "onsite-input" / result["evidence_id"]
    assert (evidence / "screen.png").read_bytes().startswith(b"\x89PNG")
    record = json.loads((evidence / "receipt.json").read_text(encoding="utf-8"))
    assert record["verified"] is False and record["interaction_scope"] == "current_synthetic_page_only"
    assert TOKEN not in json.dumps(record) and "serial-never-reported" not in json.dumps(record)


@pytest.mark.parametrize("fault", ["wrong_target", "wrong_run", "stale"])
def test_failed_observation_fails_closed_without_serial_or_saved_screenshot(tmp_path, fault):
    kwargs, observer, backend, opens, private = setup(tmp_path, fault=fault)
    result = run_experiment("move", dx=4, dy=0, **kwargs)
    assert result["state"] == "NOT_SENT" and result["verified"] is False
    assert not opens and not backend.calls
    assert not list((private / "onsite-input").glob("*/screen.png"))


def test_ack_is_never_reported_as_verified_and_action_evidence_is_private(tmp_path):
    kwargs, observer, backend, opens, private = setup(tmp_path)
    result = run_experiment("move", dx=4, dy=-2, **kwargs)
    assert result["state"] == "executed" and result["verified"] is False
    assert len(backend.calls) == 1 and backend.closed == 1 and opens == [True]
    record = json.loads((private / "onsite-input" / result["evidence_id"] / "receipt.json").read_text())
    assert record["operation"] == "move" and record["verified"] is False
    assert (private / "onsite-input" / result["evidence_id"] / "before.png").exists()
    assert (private / "onsite-input" / result["evidence_id"] / "after.png").exists()
    assert (private / "onsite-input" / result["evidence_id"] / "page-association.png").exists()
    events = json.loads((private / "onsite-input" / result["evidence_id"] / "events.json").read_text())
    assert events["events"] == [] and events["truncated"] is False
    assert TOKEN not in json.dumps(record) and "serial-never-reported" not in json.dumps(record)


def test_acknowledged_action_archives_verified_before_frame_after_it_ages(tmp_path):
    kwargs, observer, backend, opens, private = setup(tmp_path)
    backend.on_execute = lambda: kwargs["clock"].advance(1.4)
    result = run_experiment("move", dx=2, dy=1, **kwargs)
    assert result["state"] == "executed" and result["verified"] is False
    assert len(backend.calls) == 1 and backend.closed == 1
    evidence = private / "onsite-input" / result["evidence_id"]
    assert (evidence / "before.png").exists() and (evidence / "after.png").exists()
    before = json.loads((evidence / "before-metadata.json").read_text(encoding="utf-8"))
    assert before["metadata"]["received_monotonic"] < kwargs["clock"]()
    assert before["age_at_save_seconds"] > 2
    assert before["metadata"]["state"] == "fresh"


def test_missing_pointer_sequence_and_out_of_bound_motion_never_open_backend(tmp_path):
    kwargs, observer, backend, opens, _private = setup(tmp_path)
    with pytest.raises(ValueError):
        run_experiment("click", **kwargs)
    with pytest.raises(ValueError):
        run_experiment("move", dx=17, dy=0, **kwargs)
    assert not opens and not backend.calls


def test_click_requires_current_pointer_sequence_and_scroll_is_small(tmp_path):
    kwargs, observer, backend, opens, _private = setup(tmp_path)
    observer.rows.append({"source": "client_reported_browser_event", "run_id": RUN_ID,
        "test_id": "ble-round-01", "seq": 9, "recorded_monotonic": 10.,
        "event": {"type": "pointermove", "isTrusted": True, "target": "12",
                  "clientX": 200, "clientY": 300, "button": -1}})
    wrong = run_experiment("click", pointer_seq=8, **kwargs)
    assert wrong["state"] == "NOT_SENT" and not backend.calls
    right = run_experiment("click", pointer_seq=9, **kwargs)
    assert right["state"] == "executed" and right["verified"] is False
    assert len(backend.calls) == 1


def test_transport_failure_after_dispatch_is_unknown_and_never_retried(tmp_path):
    backend = FakeBackend({"state": "unknown"})
    kwargs, observer, backend, opens, _private = setup(tmp_path, backend=backend)
    result = run_experiment("move", dx=1, dy=0, **kwargs)
    assert result["state"] == "UNKNOWN_OR_NOT_SENT" and result["verified"] is False
    assert len(backend.calls) == 1 and opens == [True]


def test_scroll_requires_current_pointer_event_and_is_bounded(tmp_path):
    kwargs, observer, backend, opens, _private = setup(tmp_path)
    observer.rows.append({"source": "client_reported_browser_event", "run_id": RUN_ID,
        "test_id": "ble-round-01", "seq": 9, "recorded_monotonic": 10.,
        "event": {"type": "pointermove", "isTrusted": True, "target": "probe-grid",
                  "clientX": 200, "clientY": 300, "button": -1}})
    with pytest.raises(ValueError):
        run_experiment("scroll", pointer_seq=9, vertical=4, **kwargs)
    assert not opens and not backend.calls
    result = run_experiment("scroll", pointer_seq=9, vertical=-3, **kwargs)
    assert result["state"] == "executed" and result["verified"] is False
    assert backend.calls[0]["op"] == "scroll"
    assert backend.calls[0]["args"] == {"v": -3, "h": 0}


def test_event_mismatch_or_unknown_operation_does_not_open_backend(tmp_path):
    kwargs, observer, backend, opens, _private = setup(tmp_path)
    observer.rows.append({"source": "client_reported_browser_event", "run_id": RUN_ID,
        "test_id": "ble-round-01", "seq": 9, "recorded_monotonic": 10.,
        "event": {"type": "pointermove", "isTrusted": True, "target": "probe-grid",
                  "clientX": 200, "clientY": 300, "button": -1}})
    observer.events = lambda _run: {"run": {"run_id": RUN_ID, "nonce": NONCE,
        "test_id": "ble-round-01", "seed": "42", "phone_verified": False}, "events": []}
    result = run_experiment("click", pointer_seq=9, **kwargs)
    assert result["state"] == "NOT_SENT" and not opens and not backend.calls
    with pytest.raises(ValueError):
        run_experiment("type", **kwargs)


def test_cli_output_is_compact_and_does_not_include_control_token(monkeypatch, capsys, tmp_path):
    private_result = {"state": "observed", "verified": False, "evidence_id": "20261013T000000000000Z",
                      "evidence_path": str(tmp_path / "private" / "onsite-input" / "20261013T000000000000Z")}
    monkeypatch.setattr(page_experiments, "run_experiment", lambda *args, **kwargs: private_result)
    assert page_experiments.main(["observe"]) == 0
    output = capsys.readouterr().out
    assert output.strip() == json.dumps(private_result, sort_keys=True, separators=(",", ":"))
    assert TOKEN not in output and "runToken" not in output and "serial" not in output


@pytest.mark.parametrize("phase", ["entry", "target", "page", "open"])
def test_stop_generation_is_captured_before_preparation_and_never_rearmed(tmp_path, phase):
    from phonelab.stop_signal import StopStore
    kwargs, observer, backend, opens, private = setup(tmp_path)
    store = StopStore(kwargs["private_root"])
    token = store.capture()
    if phase == "entry": store.publish_stop()
    if phase == "target":
        def target(_root): store.publish_stop(); return ("private-serial", TARGET)
        kwargs["target_reader"] = target
    if phase == "page":
        original = observer.visible_run
        def visible(**values): store.publish_stop(); return original(**values)
        observer.visible_run = visible
    if phase == "open":
        def open_stopped(): store.publish_stop(); return backend
        kwargs["backend_opener"] = open_stopped
    result = run_experiment("move", dx=1, dy=0, stop_token=token, **kwargs)
    assert result["state"] == "NOT_SENT"
    assert backend.calls == []
    assert backend.closed == (1 if phase == "open" else 0)
    again = run_experiment("move", dx=1, dy=0, stop_token=token, **kwargs)
    assert again["state"] == "NOT_SENT" and backend.calls == []


def test_image_click_uses_local_pixels_and_no_browser_hover_coordinates(tmp_path, monkeypatch):
    import phonelab.image_probe_guard as guard_module
    import phonelab.local_ocr as ocr_module
    kwargs, observer, backend, opens, private = setup(tmp_path)
    checked = []
    transport = []
    class Guard:
        def __init__(self, probe, png, nonce, fields):
            assert probe == "10" and nonce == NONCE
            assert fields['synthetic'] is True
        def verify(self, png):
            assert transport == ["hello"]
            checked.append(png)
            return {"probe_id": "10", "count": 0}
    monkeypatch.setattr(guard_module, "ImageProbeGuard", Guard)
    monkeypatch.setattr(ocr_module, "read_probe_ocr", lambda *_: {"synthetic":True,
        "visible_probes":[{"id":"10", "count":0}]})
    def execute(request):
        transport.append("hello")
        request["_pre_input_guard"]()
        transport.append("requested_operation")
        backend.calls.append(request)
        return {"state":"executed", "ble_connected":True}
    backend.execute = execute
    observer.rows = []
    result = run_experiment("image-click", probe_id="10", expected_count=0, **kwargs)
    assert result["state"] == "executed" and result["verified"] is False
    assert len(backend.calls) == 1 and backend.calls[0]["op"] == "click"
    assert backend.calls[0]["args"] == {}  # no browser/event coordinates reach the image path
    assert len(checked) == 1 and transport == ["hello", "requested_operation"]
    assert backend.closed == 1


def test_image_click_refuses_old_expected_count_before_open(tmp_path, monkeypatch):
    import phonelab.local_ocr as ocr_module
    kwargs, observer, backend, opens, private = setup(tmp_path)
    monkeypatch.setattr(ocr_module, "read_probe_ocr", lambda *_: {"visible_probes":[{"id":"10", "count":1}]})
    result = run_experiment("image-click", probe_id="10", expected_count=0, **kwargs)
    assert result["state"] == "NOT_SENT" and not opens and not backend.calls


def test_image_scroll_requires_expected_count_and_small_nonzero_vertical_delta():
    valid = page_experiments._validate_request("image-scroll", probe_id="10",
                                               expected_count=4, vertical=-3)
    assert valid == {"probe_id": "10", "expected_count": 4, "vertical": -3}
    for kwargs in [
        {"probe_id": "10", "vertical": 1},
        {"probe_id": "10", "expected_count": 0, "vertical": 0},
        {"probe_id": "10", "expected_count": 0, "vertical": 4},
        {"probe_id": "00", "expected_count": 0, "vertical": 1},
        {"probe_id": "10", "expected_count": -1, "vertical": 1},
        {"probe_id": "10", "expected_count": 0, "vertical": 1, "pointer_seq": 1},
    ]:
        with pytest.raises(ValueError):
            page_experiments._validate_request("image-scroll", **kwargs)


def test_image_scroll_rechecks_image_and_sends_only_scroll_wire(tmp_path, monkeypatch):
    import phonelab.image_probe_guard as guard_module
    import phonelab.local_ocr as ocr_module
    kwargs, observer, backend, opens, private = setup(tmp_path)
    checked = []
    transport = []
    class Guard:
        def __init__(self, probe, png, nonce, fields):
            assert probe == "10" and nonce == NONCE
        def verify(self, png):
            assert transport == ["hello"]
            checked.append(png)
            return {"probe_id": "10", "count": 3}
    monkeypatch.setattr(guard_module, "ImageProbeGuard", Guard)
    monkeypatch.setattr(ocr_module, "read_probe_ocr", lambda *_: {"visible_probes": [
        {"id": "10", "count": 3, "image_text_bounds": {"x": 1, "y": 2, "width": 3, "height": 4}}]})
    def execute(request):
        transport.append("hello")
        request["_pre_input_guard"]()
        transport.append("requested_operation")
        backend.calls.append(request)
        return {"state": "executed", "ble_connected": True}
    backend.execute = execute
    observer.rows = []
    result = run_experiment("image-scroll", probe_id="10", expected_count=3, vertical=2, **kwargs)
    assert result["state"] == "executed" and result["verified"] is False
    request = backend.calls[0]
    assert request["op"] == "scroll" and request["args"] == {"v": 2, "h": 0}
    assert request["_dispatch_ttl_seconds"] == 1
    assert len(checked) == 1 and transport == ["hello", "requested_operation"]
    assert backend.closed == 1


def test_image_scroll_stale_expected_count_refuses_before_serial_open(tmp_path, monkeypatch):
    import phonelab.local_ocr as ocr_module
    kwargs, observer, backend, opens, private = setup(tmp_path)
    monkeypatch.setattr(ocr_module, "read_probe_ocr", lambda *_: {"visible_probes": [
        {"id": "10", "count": 4}]})
    result = run_experiment("image-scroll", probe_id="10", expected_count=3, vertical=-1, **kwargs)
    assert result["state"] == "NOT_SENT" and not opens and not backend.calls


def test_image_scroll_cli_requires_probe_count_and_vertical():
    args = page_experiments._parser().parse_args([
        "image-scroll", "--probe-id", "10", "--expected-count", "2", "--vertical", "-1"])
    assert args.operation == "image-scroll" and args.expected_count == 2 and args.vertical == -1
