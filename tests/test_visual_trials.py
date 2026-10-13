"""Offline behavior tests for bounded, image-planned synthetic-page trials."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from phonelab.visual_trials import plan_pointer_step, run_trials, validate_trial_request
import phonelab.visual_trials as visual_trials


def ocr(count=0, *, probes=None, summary=None):
    return {"status": "OK", "summary_clicks": count if summary is None else summary,
            "summary_last": "none" if count == 0 else "01",
            "visible_probes": probes if probes is not None else [
                {"id": "01", "count": count,
                 "image_text_bounds": {"x": 100, "y": 300, "width": 80, "height": 20}}]}


def test_request_limits_reject_bad_targets_rounds_and_duplicate_targets():
    assert validate_trial_request(["01", "25"], 2) == ("01", "25")
    for probes, rounds in [([], 1), (["00"], 1), (["01", "01"], 1), (["01"], 3), (["01"] * 26, 1)]:
        with pytest.raises(ValueError):
            validate_trial_request(probes, rounds)


def test_ocr_uses_only_windows_powershell5_and_suppresses_stderr(monkeypatch, tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"png")
    calls = []
    def which(name):
        assert name == "powershell.exe"
        return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(ocr()))
    monkeypatch.setattr(visual_trials.shutil, "which", which)
    monkeypatch.setattr(visual_trials.subprocess, "run", fake_run)
    result = visual_trials._run_ocr(image, "nonce")
    assert result["status"] == "OK"
    args, kwargs = calls[0]
    assert Path(args[0]).name.lower() == "powershell.exe"
    assert args[1] == '-NoProfile' and '-File' in args
    assert "pwsh" not in " ".join(args).lower()
    assert args[args.index('-ExecutionPolicy') + 1] == 'RemoteSigned'
    assert kwargs["timeout"] == 30 and kwargs["stderr"] is visual_trials.subprocess.DEVNULL


def test_ocr_does_not_fallback_to_powershell7_or_print_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(visual_trials.shutil, "which", lambda name: None if name == "powershell.exe" else pytest.fail(name))
    with pytest.raises(RuntimeError):
        visual_trials._run_ocr(tmp_path / "missing.png", "nonce")


def test_execute_main_uses_new_matrix_subdirectory_and_never_prints_local_token(monkeypatch, capsys, tmp_path):
    data = tmp_path / "data"
    private = data / "private"
    private.mkdir(parents=True)
    secret = "local-test-token-never-output"
    (private / "runtime.json").write_text(json.dumps({"token": secret}), encoding="utf-8")
    monkeypatch.setattr(visual_trials.deploy, "DATA", data)
    monkeypatch.setattr(visual_trials, "read_target_binding", lambda _root: ("private-serial", "iphone-ac39233f6b70"))

    class Client:
        def visible_run(self, *, test_id, seed):
            return {"run_id": "0123456789abcdef", "nonce": "nonce", "test_id": test_id,
                    "seed": seed, "last_seq": 0, "phone_verified": False}
    monkeypatch.setattr(visual_trials, "LiveObservationClient", lambda *_args, **_kwargs: Client())
    def observe(operation, **kwargs):
        assert operation == "observe"
        evidence = private / "onsite-input" / "initial"
        evidence.mkdir(parents=True)
        (evidence / "screen.png").write_bytes(b"png")
        (evidence / "receipt.json").write_text(json.dumps({"run_id": "0123456789abcdef"}), encoding="utf-8")
        return {"state": "observed", "evidence_path": str(evidence)}
    monkeypatch.setattr(visual_trials, "run_experiment", observe)
    monkeypatch.setattr(visual_trials, "_run_ocr", lambda *_args: ocr())
    actual_run_trials = visual_trials.run_trials
    def fake_trials(probes, rounds, **kwargs):
        matrix = kwargs["evidence_root"]
        assert not matrix.exists()
        assert kwargs["initial_image"].is_file()
        return actual_run_trials(probes, rounds, execute=False,
            initial_image=kwargs["initial_image"], initial_ocr=kwargs["initial_ocr"],
            run_context=kwargs["run_context"], observer=kwargs["observer"],
            experiment=kwargs["experiment"], ocr_reader=kwargs["ocr_reader"],
            stop_token=None, evidence_root=matrix)
    monkeypatch.setattr(visual_trials, "run_trials", fake_trials)
    assert visual_trials.main(["--execute", "--probes", "02"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "observed" and secret not in json.dumps(output)
    trial_root = Path(output["evidence_path"]).parent
    assert (trial_root / "initial.png").is_file()
    assert (trial_root / "initial-observation.json").is_file()
    assert (trial_root / "matrix").is_dir()


def test_planner_uses_png_pointer_and_ocr_only_and_moves_in_small_feedback_steps():
    calls = []
    pointer = SimpleNamespace(center_x=70., center_y=80.)
    plan = plan_pointer_step("01", b"validated-png", "nonce", ocr(),
                             pointer_fn=lambda png, nonce: calls.append((png, nonce)) or pointer)
    assert calls == [(b"validated-png", "nonce")]
    assert plan["kind"] == "move" and plan["dx"] == 16 and plan["dy"] == 16
    assert plan["goal"] == (140., 342.)
    assert "events" not in plan


def test_planner_clicks_only_when_unique_target_is_within_ten_pixels():
    pointer = SimpleNamespace(center_x=140., center_y=342.)
    plan = plan_pointer_step("01", b"png", "nonce", ocr(), pointer_fn=lambda *_: pointer)
    assert plan["kind"] == "click"
    duplicate = ocr(probes=[ocr()["visible_probes"][0], ocr()["visible_probes"][0]])
    assert plan_pointer_step("01", b"png", "nonce", duplicate,
                             pointer_fn=lambda *_: pointer)["kind"] == "stop"


def test_ambiguous_pointer_gets_at_most_two_wakes_then_stops():
    none = lambda *_: None
    assert plan_pointer_step("01", b"png", "nonce", ocr(), pointer_fn=none, wake_count=0)["kind"] == "wake"
    assert plan_pointer_step("01", b"png", "nonce", ocr(), pointer_fn=none, wake_count=1)["kind"] == "wake"
    assert plan_pointer_step("01", b"png", "nonce", ocr(), pointer_fn=none, wake_count=2)["kind"] == "stop"


class FakeStopToken:
    def __init__(self): self.stopped = False
    def is_set(self): return self.stopped
    def set(self): self.stopped = True


class FakeObserver:
    def __init__(self):
        self.seq = 1
        self.rows = [{"seq": 1, "source": "client_reported_browser_event", "run_id": "0123456789abcdef",
                      "test_id": "ble-round-01", "recorded_monotonic": 10.,
                      "event": {"type": "pointermove", "isTrusted": True, "target": "01",
                                "clientX": 0, "clientY": 0, "button": -1}}]
    def events(self, run_id):
        return {"run": {"run_id": run_id, "nonce": "nonce", "test_id": "ble-round-01",
                         "seed": "42", "last_seq": self.rows[-1]["seq"], "phone_verified": False},
                "events": list(self.rows)}
    def snapshot(self):
        self.seq += 1
        return SimpleNamespace(image_bytes=b"fake-png", metadata={"target": "iphone-abc", "source": "USB_SCREENSHOT",
            "sequence": self.seq, "received_monotonic": 10., "width": 828, "height": 1792,
            "orientation": "portrait", "source_time": None, "locked": "UNKNOWN", "fresh": True, "state": "fresh"})


def make_evidence(root, n, run_id="0123456789abcdef", state="executed"):
    path = root / f"evidence-{n}"
    path.mkdir()
    (path / "before.png").write_bytes(b"before")
    (path / "after.png").write_bytes(b"after")
    (path / "events.json").write_text("{}", encoding="utf-8")
    (path / "receipt.json").write_text(json.dumps({"state": state, "verified": False,
        "operation": "click", "run_id": run_id}), encoding="utf-8")
    return {"state": state, "verified": False, "evidence_path": str(path), "evidence_id": path.name}


def run_fake(tmp_path, *, token=None, effect=True, tamper_target=False):
    token = token or FakeStopToken()
    observer = FakeObserver()
    action_calls = []
    ocr_count = {"value": 0}

    def experiment(operation, **kwargs):
        action_calls.append((operation, kwargs))
        assert kwargs["stop_token"] is token
        if token.is_set():
            return {"state": "NOT_SENT", "verified": False}
        if operation == "click":
            if effect: ocr_count["value"] += 1
            observer.rows.extend([
                {"seq": seq, "source": "client_reported_browser_event", "run_id": "0123456789abcdef",
                 "test_id": "ble-round-01", "recorded_monotonic": 10.,
                 "event": {"type": name, "isTrusted": True,
                           "target": "02" if tamper_target else "01"}}
                for seq, name in enumerate(("pointerdown", "pointerup", "click"), start=2)])
        return make_evidence(tmp_path, len(action_calls))

    def ocr_reader(path, nonce):
        return ocr(ocr_count["value"])

    delays = []
    def sleep(seconds): delays.append(seconds)
    initial_image = tmp_path / "initial.png"
    initial_image.write_bytes(b"initial")
    result = run_trials(["01"], 1, execute=True, initial_image=initial_image,
        initial_ocr=ocr(), run_context={"run_id": "0123456789abcdef", "nonce": "nonce",
        "test_id": "ble-round-01", "seed": "42", "target": "iphone-abc"},
        observer=observer, experiment=experiment, ocr_reader=ocr_reader, pointer_fn=lambda *_: SimpleNamespace(center_x=140., center_y=342.),
        stop_token=token, evidence_root=tmp_path / "trial", clock=lambda: 10., sleep=sleep,
        snapshot_validator=lambda snapshot, **kwargs: snapshot)
    return result, action_calls, token


def test_click_requires_latest_trusted_pointermove_exact_target_and_never_uses_event_coordinates(tmp_path):
    initial_image = tmp_path / "initial.png"
    initial_image.write_bytes(b"initial")
    observer = FakeObserver()
    observer.rows[-1]["event"]["target"] = "02"
    token = FakeStopToken()
    action_calls = []
    result = run_trials(["01"], 1, execute=True, initial_image=initial_image, initial_ocr=ocr(),
        run_context={"run_id": "0123456789abcdef", "nonce": "nonce", "test_id": "ble-round-01", "seed": "42", "target": "iphone-abc"},
        observer=observer, experiment=lambda op, **kw: action_calls.append((op, kw)) or {},
        ocr_reader=lambda *_: ocr(), pointer_fn=lambda *_: SimpleNamespace(center_x=140., center_y=342.),
        stop_token=token, evidence_root=tmp_path / "trial", clock=lambda: 10., sleep=lambda _: None,
        snapshot_validator=lambda snapshot, **kwargs: snapshot)
    assert result["state"] == "stopped" and not action_calls


def test_read_only_mode_never_calls_experiment_or_stop_token(tmp_path):
    initial = tmp_path / "initial.png"
    initial.write_bytes(b"initial")
    result = run_trials(["01"], 1, execute=False, initial_image=initial, initial_ocr=ocr(),
        run_context={"run_id": "0123456789abcdef", "nonce": "nonce", "test_id": "ble-round-01",
                     "seed": "42", "target": "iphone-abc"}, observer=FakeObserver(),
        experiment=lambda *_args, **_kwargs: pytest.fail("read-only mode attempted an action"),
        ocr_reader=lambda *_: ocr(), pointer_fn=lambda *_: pytest.fail("read-only mode planned a pointer"),
        stop_token=None, evidence_root=tmp_path / "trial")
    assert result["state"] == "observed" and result["actions"] == 0


def test_click_effect_is_independently_checked_and_stop_prevents_next_round(tmp_path):
    result, calls, token = run_fake(tmp_path)
    assert result["state"] == "completed" and result["verified_clicks"] == 1
    assert [item[0] for item in calls] == ["click"]
    assert token.is_set() is False


def test_shared_stop_during_one_shot_prevents_verification_or_next_round(tmp_path):
    token = FakeStopToken()
    # Reuse the complete independent event/OCR fake, but publish cancellation
    # from inside the first action after its single ACK.
    def controlled_run(root):
        token2 = token
        observer = FakeObserver()
        calls = []
        count = {"value": 0}
        initial = root / "initial.png"
        initial.write_bytes(b"initial")
        def action(operation, **kwargs):
            calls.append(operation)
            count["value"] += 1
            observer.rows.extend([
                {"seq": seq, "source": "client_reported_browser_event", "run_id": "0123456789abcdef",
                 "test_id": "ble-round-01", "recorded_monotonic": 10.,
                 "event": {"type": name, "isTrusted": True, "target": "01"}}
                for seq, name in enumerate(("pointerdown", "pointerup", "click"), start=2)])
            token2.set()
            return make_evidence(root, 1)
        result = run_trials(["01"], 2, execute=True, initial_image=initial, initial_ocr=ocr(),
            run_context={"run_id": "0123456789abcdef", "nonce": "nonce", "test_id": "ble-round-01",
                         "seed": "42", "target": "iphone-abc"}, observer=observer,
            experiment=action, ocr_reader=lambda *_: ocr(count["value"]),
            pointer_fn=lambda *_: SimpleNamespace(center_x=140., center_y=342.),
            stop_token=token2, evidence_root=root / "trial", clock=lambda: 10., sleep=lambda _: None,
            snapshot_validator=lambda snapshot, **kwargs: snapshot)
        return result, calls
    result, calls = controlled_run(tmp_path)
    assert result["state"] == "stopped" and result["verified_clicks"] == 0
    assert calls == ["click"] and token.is_set()


def test_ack_without_ocr_effect_stops_without_replay(tmp_path):
    result, calls, _ = run_fake(tmp_path, effect=False)
    assert result["state"] != "completed"
    assert [item[0] for item in calls] == ["click"]


def test_click_event_to_wrong_target_stops_even_if_ocr_count_changed(tmp_path):
    result, calls, _ = run_fake(tmp_path, tamper_target=True)
    assert result["state"] != "completed"
    assert [item[0] for item in calls] == ["click"]
