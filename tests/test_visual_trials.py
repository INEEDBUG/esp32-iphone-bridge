"""Offline behavior tests for bounded, image-planned synthetic-page trials."""
import json
import math
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
        assert kwargs["click_mode"] == "image"
        matrix = kwargs["evidence_root"]
        assert not matrix.exists()
        assert kwargs["initial_image"].is_file()
        return actual_run_trials(probes, rounds, execute=False,
            initial_image=kwargs["initial_image"], initial_ocr=kwargs["initial_ocr"],
            run_context=kwargs["run_context"], observer=kwargs["observer"],
            experiment=kwargs["experiment"], ocr_reader=kwargs["ocr_reader"],
            stop_token=None, evidence_root=matrix)
    monkeypatch.setattr(visual_trials, "run_trials", fake_trials)
    assert visual_trials.main(["--execute", "--image-clicks", "--probes", "02"]) == 0
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
    assert plan["kind"] == "move" and plan["dx"] == 0 and plan["dy"] == 16
    assert abs(plan["dx"]) <= 16 and abs(plan["dy"]) <= 16
    assert plan["goal"] == (140., 342.)
    assert "events" not in plan


def test_planner_clicks_only_when_unique_target_is_within_ten_pixels():
    pointer = SimpleNamespace(center_x=140., center_y=342.)
    plan = plan_pointer_step("01", b"png", "nonce", ocr(), pointer_fn=lambda *_: pointer)
    assert plan["kind"] == "click"
    duplicate = ocr(probes=[ocr()["visible_probes"][0], ocr()["visible_probes"][0]])
    assert plan_pointer_step("01", b"png", "nonce", duplicate,
                             pointer_fn=lambda *_: pointer)["kind"] == "stop"


def _simulate_label_avoiding_route(start, bounds, *, gain=1.0):
    x, y, width, height = bounds
    target_ocr = ocr(probes=[{"id": "12", "count": 0,
        "image_text_bounds": {"x": x, "y": y, "width": width, "height": height}}])
    position = tuple(map(float, start))
    goal = (x + width / 2.0, y + height + 22.0)
    expanded = (x - 20.0, y - 20.0, x + width + 20.0, y + height + 20.0)
    steps = 0
    while steps < 40:
        plan = plan_pointer_step("12", b"png", "nonce", target_ocr,
            pointer_fn=lambda *_: SimpleNamespace(center_x=position[0], center_y=position[1]))
        if plan["kind"] == "click":
            assert math.dist(position, goal) <= 10
            return steps, position
        assert plan["kind"] == "move", plan
        dx, dy = plan["dx"], plan["dy"]
        assert (dx, dy) != (0, 0)
        assert abs(dx) <= 16 and abs(dy) <= 16
        assert dx == 0 or dy == 0  # detour legs cannot cut diagonally across text
        next_position = (position[0] + dx * gain, position[1] + dy * gain)
        # The pointer center's segment stays outside the label's radius margin.
        if dx:
            assert not (expanded[1] < position[1] < expanded[3]
                        and max(min(position[0], next_position[0]), expanded[0])
                        < min(max(position[0], next_position[0]), expanded[2]))
        if dy:
            assert not (expanded[0] < position[0] < expanded[2]
                        and max(min(position[1], next_position[1]), expanded[1])
                        < min(max(position[1], next_position[1]), expanded[3]))
        position = next_position
        steps += 1
    pytest.fail("route did not converge")


def test_planner_routes_around_label_with_bounded_convergent_axis_moves():
    # Physical geometry: a direct diagonal from (318,1223) to (413.5,1290)
    # crosses Probe 12's label. Its clear x lane permits vertical-first travel.
    steps, final = _simulate_label_avoiding_route((318, 1223), (347, 1247, 133, 21))
    assert steps < 20 and math.dist(final, (413.5, 1290)) <= 10

    # A cursor already below the label must approach horizontally before the
    # small vertical correction; it must not bounce out to a side lane.
    steps, final = _simulate_label_avoiding_route((343, 1298), (347, 1247, 133, 21))
    assert steps < 10 and math.dist(final, (413.5, 1290)) <= 10


def test_label_avoiding_route_stays_safe_and_converges_at_15x_movement_gain():
    # The device has shown roughly 1.5px movement per requested unit. Check
    # each intermediate segment, since the next planner call is also the
    # live target-visibility stop check.
    steps, final = _simulate_label_avoiding_route(
        (318, 1223), (347, 1247, 133, 21), gain=1.5)
    assert 0 < steps < 20
    assert math.dist(final, (413.5, 1290)) <= 10

    steps, final = _simulate_label_avoiding_route(
        (343, 1298), (347, 1247, 133, 21), gain=1.5)
    assert 0 < steps < 20
    assert math.dist(final, (413.5, 1290)) <= 10


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
                         "seed": "42", "last_seq": self.rows[-1]["seq"] if self.rows else 0, "phone_verified": False},
                "events": list(self.rows)}
    def snapshot(self):
        self.seq += 1
        return SimpleNamespace(image_bytes=b"fake-png", metadata={"target": "iphone-abc", "source": "USB_SCREENSHOT",
            "sequence": self.seq, "received_monotonic": 10., "width": 828, "height": 1792,
            "orientation": "portrait", "source_time": None, "locked": "UNKNOWN", "fresh": True, "state": "fresh"})


def make_evidence(root, n, run_id="0123456789abcdef", state="executed", operation="click"):
    path = root / f"evidence-{n}"
    path.mkdir()
    (path / "before.png").write_bytes(b"before")
    (path / "after.png").write_bytes(b"after")
    (path / "events.json").write_text("{}", encoding="utf-8")
    (path / "receipt.json").write_text(json.dumps({"state": state, "verified": False,
        "operation": operation, "run_id": run_id}), encoding="utf-8")
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


def test_image_click_mode_needs_no_pointermove_and_accepts_target_only_delayed_proof(tmp_path):
    token = FakeStopToken()
    observer = FakeObserver()
    observer.rows = []  # Deliberately no hover/pointermove event before action.
    calls = []
    ocr_reads = {"count": 0}

    def action(operation, **kwargs):
        calls.append((operation, dict(kwargs)))
        assert operation == "image-click"
        assert kwargs["probe_id"] == "01" and kwargs["expected_count"] == 0
        assert kwargs["stop_token"] is token
        observer.rows.extend([
            {"seq": seq, "source": "client_reported_browser_event", "run_id": "0123456789abcdef",
             "test_id": "ble-round-01", "recorded_monotonic": 10.,
             "event": {"type": name, "isTrusted": True, "target": "01"}}
            for seq, name in enumerate(("pointerdown", "pointerup", "click"), start=1)])
        return make_evidence(tmp_path, 1, operation=operation)

    def partial_ocr(_path, _nonce):
        ocr_reads["count"] += 1
        count = 0 if ocr_reads["count"] == 1 else 1
        return {"status": "PARTIAL", "summary_clicks": None, "summary_last": None,
                "visible_probes": [{"id": "01", "count": count,
                    "image_text_bounds": {"x": 100, "y": 300, "width": 80, "height": 20}}]}

    initial = tmp_path / "initial.png"
    initial.write_bytes(b"png")
    result = run_trials(["01"], 1, execute=True, initial_image=initial,
        initial_ocr={"status": "PARTIAL", "summary_clicks": None, "summary_last": None,
            "visible_probes": [{"id": "01", "count": 0,
                "image_text_bounds": {"x": 100, "y": 300, "width": 80, "height": 20}}]},
        run_context={"run_id": "0123456789abcdef", "nonce": "nonce", "test_id": "ble-round-01",
                     "seed": "42", "target": "iphone-abc"}, observer=observer, experiment=action,
        ocr_reader=partial_ocr, pointer_fn=lambda *_: SimpleNamespace(center_x=140., center_y=342.),
        stop_token=token, evidence_root=tmp_path / "trial", clock=lambda: 10., sleep=lambda _: None,
        snapshot_validator=lambda snapshot, **kwargs: snapshot, click_mode="image")
    assert result["state"] == "completed" and result["verified_clicks"] == 1
    assert calls[0][0] == "image-click" and calls[0][1]["expected_count"] == 0
    assert result["summary_verified"] is None


def test_image_click_mode_missing_target_or_unconfirmed_click_never_replays(tmp_path):
    initial = tmp_path / "initial.png"
    initial.write_bytes(b"png")
    context = {"run_id": "0123456789abcdef", "nonce": "nonce", "test_id": "ble-round-01",
               "seed": "42", "target": "iphone-abc"}
    calls = []
    missing = run_trials(["01"], 1, execute=True, initial_image=initial,
        initial_ocr={"status": "PARTIAL", "visible_probes": []}, run_context=context,
        observer=FakeObserver(), experiment=lambda *a, **kw: calls.append((a, kw)),
        ocr_reader=lambda *_: pytest.fail("missing target should stop before OCR/action"),
        pointer_fn=lambda *_: SimpleNamespace(center_x=140., center_y=342.), stop_token=FakeStopToken(),
        evidence_root=tmp_path / "missing", clock=lambda: 10., click_mode="image")
    assert missing["state"] == "stopped" and not calls

    observer = FakeObserver()
    observer.rows = []
    def false_action(operation, **kwargs):
        calls.append(operation)
        # No trusted events: ACK/evidence alone cannot prove click effect.
        return make_evidence(tmp_path, len(calls), operation=operation)
    false_click = run_trials(["01"], 1, execute=True, initial_image=initial,
        initial_ocr={"status": "PARTIAL", "summary_clicks": None, "visible_probes": ocr()["visible_probes"]},
        run_context=context, observer=observer, experiment=false_action, ocr_reader=lambda *_: ocr(),
        pointer_fn=lambda *_: SimpleNamespace(center_x=140., center_y=342.), stop_token=FakeStopToken(),
        evidence_root=tmp_path / "false", clock=lambda: 10., sleep=lambda _: None,
        snapshot_validator=lambda snapshot, **kwargs: snapshot, click_mode="image")
    assert false_click["state"] == "stopped" and calls == ["image-click"]


def test_image_click_mode_checks_visible_summary_delta_and_last_target(tmp_path):
    observer = FakeObserver()
    observer.rows = []
    token = FakeStopToken()
    calls = []
    ocr_reads = {"n": 0}
    def action(operation, **kwargs):
        calls.append(operation)
        observer.rows.extend([
            {"seq": seq, "source": "client_reported_browser_event", "run_id": "0123456789abcdef",
             "test_id": "ble-round-01", "recorded_monotonic": 10.,
             "event": {"type": name, "isTrusted": True, "target": "01"}}
            for seq, name in enumerate(("pointerdown", "pointerup", "click"), start=1)])
        return make_evidence(tmp_path, 1, operation=operation)
    def wrong_summary(_path, _nonce):
        ocr_reads["n"] += 1
        return {"status": "OK", "summary_clicks": 0 if ocr_reads["n"] == 1 else 2,
                "summary_last": "01", "visible_probes": [{"id": "01",
                "count": 0 if ocr_reads["n"] == 1 else 1,
                "image_text_bounds": {"x": 100, "y": 300, "width": 80, "height": 20}}]}
    initial=tmp_path/'initial.png';initial.write_bytes(b'png')
    result=run_trials(["01"],1,execute=True,initial_image=initial,
        initial_ocr={"status":"OK","summary_clicks":0,"summary_last":"none",
            "visible_probes":ocr()["visible_probes"]},
        run_context={"run_id":"0123456789abcdef","nonce":"nonce","test_id":"ble-round-01",
                     "seed":"42","target":"iphone-abc"},observer=observer,experiment=action,
        ocr_reader=wrong_summary,pointer_fn=lambda *_:SimpleNamespace(center_x=140.,center_y=342.),
        stop_token=token,evidence_root=tmp_path/'summary',clock=lambda:10.,sleep=lambda _:None,
        snapshot_validator=lambda snapshot,**kwargs:snapshot,click_mode="image")
    assert result["state"] == "stopped" and calls == ["image-click"]


def test_image_click_option_is_explicit_and_does_not_imply_execution():
    assert visual_trials._parser().parse_args(["--image-clicks"]).image_clicks is True
    assert visual_trials._parser().parse_args([]).image_clicks is False
