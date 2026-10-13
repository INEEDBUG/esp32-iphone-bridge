"""Bounded, image-driven trials on the synthetic PhoneLab probe page only.

This layer never plans from DOM/event coordinates. Every movement/click remains
an independent `page_experiments.run_experiment` one-shot with a shared stop
generation; screenshots and OCR artifacts stay in the task-private tree.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from phonelab import deploy
from phonelab.iphone import read_target_binding
from phonelab.live_observation import BASE_URL, LiveObservationClient
from phonelab.page_experiments import (ROOT, TEST_ID, SEED, _load_control_token,
                                       _private_root, _validate_snapshot, run_experiment)
from phonelab.stop_signal import StopStore
from phonelab.synthetic_vision import detect_pointer
from phonelab.visual_marker import decode_visual_marker


MAX_MOVES_PER_PROBE = 60
MAX_ACTIONS = 600
MAX_RUNTIME_SECONDS = 1800
MAX_MOVE_AXIS = 16
CLICK_TOLERANCE_PX = 10.0
CLICK_VERIFY_POLLS = 16
CLICK_VERIFY_INTERVAL = 0.5
_PROBE = re.compile(r"^(0[1-9]|1[0-9]|2[0-5])$")


def validate_trial_request(probes, rounds):
    if (not isinstance(probes, (list, tuple)) or not 1 <= len(probes) <= 25
            or any(not isinstance(item, str) or not _PROBE.fullmatch(item) for item in probes)
            or len(set(probes)) != len(probes)):
        raise ValueError("choose 1 to 25 distinct probe IDs from 01 through 25")
    if type(rounds) is not int or rounds not in (1, 2):
        raise ValueError("rounds must be 1 or 2")
    return tuple(probes)


def _pointer_from_png(png, nonce):
    marker = decode_visual_marker(png, nonce)
    return detect_pointer(png, verified_marker=marker)


def _target_bounds(ocr_result, probe_id):
    if not isinstance(ocr_result, dict) or ocr_result.get("status") not in {"OK", "PARTIAL"}:
        return None
    rows = ocr_result.get("visible_probes")
    if not isinstance(rows, list):
        return None
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == probe_id]
    if len(matches) != 1:
        return None
    row = matches[0]
    if type(row.get("count")) is not int or row["count"] < 0:
        return None
    box = row.get("image_text_bounds")
    if not isinstance(box, dict):
        return None
    values = [box.get(name) for name in ("x", "y", "width", "height")]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        return None
    x, y, width, height = values
    if (x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 828 or y + height > 1792):
        return None
    return row, (x, y, width, height)


def plan_pointer_step(probe_id, png, nonce, ocr_result, *, pointer_fn=_pointer_from_png, wake_count=0):
    """Plan from one validated PNG pointer candidate and OCR bounds only.

    Browser event records and their coordinates are intentionally not accepted
    by this API. The caller independently checks the latest event before click.
    """
    if not isinstance(probe_id, str) or not _PROBE.fullmatch(probe_id):
        return {"kind": "stop", "reason": "invalid_target"}
    target = _target_bounds(ocr_result, probe_id)
    if target is None:
        return {"kind": "stop", "reason": "target_missing_or_ambiguous"}
    _row, (x, y, width, height) = target
    goal = (x + width / 2.0, y + height + 22.0)
    # Stay away from screen edges and Safari chrome; partially visible bottom
    # probes are deferred for a later, explicitly scoped scroll experiment.
    if not 35 < goal[0] < 793 or not 180 < goal[1] < 1450:
        return {"kind": "stop", "reason": "goal_out_of_bounds"}
    try:
        pointer = pointer_fn(png, nonce)
    except Exception:
        pointer = None
    if (pointer is None or any(type(getattr(pointer, name, None)) not in (int, float)
                               or not math.isfinite(getattr(pointer, name))
                               for name in ("center_x", "center_y"))):
        if type(wake_count) is int and 0 <= wake_count < 2:
            return {"kind": "wake", "dx": 1, "dy": 1, "goal": goal,
                    "reason": "pointer_not_unique"}
        return {"kind": "stop", "reason": "pointer_not_unique"}
    pointer_x, pointer_y = float(pointer.center_x), float(pointer.center_y)
    # Keep the pointer disc clear of the selected OCR label. Route along one
    # axis at a time: use a clear x to descend, or a row below the label to
    # approach horizontally. Re-evaluate from each fresh frame.
    margin = 20.0
    left, right = x - margin, x + width + margin
    top, bottom = y - margin, y + height + margin
    if x <= pointer_x <= x + width and y <= pointer_y <= y + height:
        return {"kind": "stop", "reason": "pointer_inside_label_safety_margin"}
    if math.hypot(goal[0] - pointer_x, goal[1] - pointer_y) <= CLICK_TOLERANCE_PX and pointer_y >= bottom:
        return {"kind": "click", "goal": goal, "pointer": (pointer_x, pointer_y),
                "pixel_error": math.hypot(goal[0] - pointer_x, goal[1] - pointer_y)}
    if pointer_y >= bottom:
        if abs(pointer_x - goal[0]) <= 0.5:
            waypoint = goal
        else:
            waypoint = (goal[0], pointer_y)
    elif pointer_x <= left or pointer_x >= right:
        waypoint = (pointer_x, goal[1])
    elif pointer_y <= top:
        candidates = [value for value in (left, right) if 35 < value < 793]
        if not candidates:
            return {"kind": "stop", "reason": "no_safe_label_lane"}
        safe_x = min(candidates, key=lambda value: abs(value - pointer_x))
        waypoint = (safe_x, pointer_y)
    elif pointer_y < y:
        waypoint = (pointer_x, top - 1.0)
    else:
        waypoint = (pointer_x, bottom + 1.0)
    error_x, error_y = waypoint[0] - pointer_x, waypoint[1] - pointer_y
    dx = max(-MAX_MOVE_AXIS, min(MAX_MOVE_AXIS, round(error_x)))
    dy = max(-MAX_MOVE_AXIS, min(MAX_MOVE_AXIS, round(error_y)))
    if dx == 0 and dy == 0:
        if error_x:
            dx = 1 if error_x > 0 else -1
        elif error_y:
            dy = 1 if error_y > 0 else -1
        else:
            return {"kind": "stop", "reason": "route_stalled"}
    return {"kind": "move", "dx": dx, "dy": dy, "goal": goal, "waypoint": waypoint,
            "pointer": (pointer_x, pointer_y),
            "pixel_error": math.hypot(error_x, error_y)}


def _validate_run_events(value, run_context):
    if not isinstance(value, dict) or not isinstance(value.get("run"), dict) or not isinstance(value.get("events"), list):
        return None
    run = value["run"]
    if any(run.get(key) != run_context.get(key) for key in ("run_id", "nonce", "test_id", "seed")):
        return None
    if run.get("phone_verified") is not False:
        return None
    return value["events"]


def _latest_pointer_sequence(observer, run_context, probe_id, *, clock):
    events = _validate_run_events(observer.events(run_context["run_id"]), run_context)
    if not events:
        return None
    row = events[-1]
    event = row.get("event", {}) if isinstance(row, dict) else {}
    recorded = row.get("recorded_monotonic") if isinstance(row, dict) else None
    if (row.get("source") != "client_reported_browser_event"
            or row.get("run_id") != run_context["run_id"]
            or row.get("test_id") != run_context["test_id"]
            or type(row.get("seq")) is not int or row["seq"] < 1
            or type(recorded) not in (int, float) or not math.isfinite(recorded)
            or not 0 <= clock() - recorded <= 10
            or event.get("type") != "pointermove" or event.get("isTrusted") is not True
            or event.get("target") != probe_id or event.get("button") != -1):
        return None
    return row["seq"]


def _event_delta_is_exact_click(before_rows, after_rows, run_context, probe_id):
    before_seq = max((row.get("seq", 0) for row in before_rows if isinstance(row, dict)), default=0)
    delta = [row for row in after_rows if isinstance(row, dict)
             and type(row.get("seq")) is int and row["seq"] > before_seq]
    clicks = [row.get("event", {}) for row in delta if row.get("event", {}).get("type") == "click"]
    downs = [row.get("event", {}) for row in delta if row.get("event", {}).get("type") == "pointerdown"]
    ups = [row.get("event", {}) for row in delta if row.get("event", {}).get("type") == "pointerup"]
    for row in delta:
        if (row.get("run_id") != run_context["run_id"] or row.get("test_id") != run_context["test_id"]):
            return False
    return (len(clicks) == len(downs) == len(ups) == 1
            and all(event.get("isTrusted") is True and event.get("target") == probe_id
                    for event in (*clicks, *downs, *ups)))


def _event_delta_has_no_click(before_rows, after_rows, run_context):
    before_seq = max((row.get("seq", 0) for row in before_rows if isinstance(row, dict)), default=0)
    for row in after_rows:
        if (not isinstance(row, dict) or type(row.get("seq")) is not int
                or row.get("run_id") != run_context["run_id"]
                or row.get("test_id") != run_context["test_id"]):
            return False
        if row["seq"] > before_seq and row.get("event", {}).get("type") in {
                "click", "pointerdown", "pointerup"}:
            return False
    return True


def _append_jsonl(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()


def _archive_action(evidence_root, action_number, result):
    if not isinstance(result, dict) or not isinstance(result.get("evidence_path"), str):
        raise ValueError("action evidence is unavailable")
    source = Path(result["evidence_path"]).resolve(strict=True)
    destination = evidence_root / "actions" / f"{action_number:04d}"
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("before.png", "after.png", "before-metadata.json", "after-metadata.json",
                 "receipt.json", "events.json"):
        file = source / name
        if file.is_file():
            shutil.copyfile(file, destination / name)
    return destination


def run_trials(probes, rounds, *, execute, initial_image, initial_ocr, run_context,
               observer, experiment, ocr_reader, pointer_fn=_pointer_from_png,
               stop_token=None, evidence_root, clock=time.monotonic, sleep=time.sleep,
               snapshot_validator=_validate_snapshot, click_verify_polls=CLICK_VERIFY_POLLS,
               click_verify_interval=CLICK_VERIFY_INTERVAL, started_monotonic=None,
               click_mode="pointer-event"):
    probes = validate_trial_request(probes, rounds)
    if click_mode not in {"pointer-event", "image"}:
        raise ValueError("unsupported click mode")
    if (type(click_verify_polls) is not int or not 1 <= click_verify_polls <= 16
            or type(click_verify_interval) not in (int, float)
            or not math.isfinite(click_verify_interval) or not 0 < click_verify_interval <= 1):
        raise ValueError("click verification wait is outside the fixed bound")
    if execute and (stop_token is None or stop_token.is_set()):
        raise ValueError("fresh explicit stop generation is required")
    for key in ("run_id", "nonce", "test_id", "seed", "target"):
        if not isinstance(run_context.get(key), str) or not run_context[key]:
            raise ValueError("bound visible run context is incomplete")
    if run_context["test_id"] != TEST_ID or run_context["seed"] != SEED:
        raise ValueError("only the approved synthetic page can be used")
    root = Path(evidence_root)
    root.mkdir(parents=True, exist_ok=False)
    steps_path = root / "steps.jsonl"
    start = clock() if started_monotonic is None else started_monotonic
    deadline = start + MAX_RUNTIME_SECONDS
    counts = {row["id"]: row["count"] for row in initial_ocr.get("visible_probes", [])
              if isinstance(row, dict) and isinstance(row.get("id"), str) and type(row.get("count")) is int}
    initial_summary = initial_ocr.get("summary_clicks")
    if execute and click_mode == "pointer-event" and (
            type(initial_summary) is not int or initial_summary < 0):
        raise ValueError("visible synthetic click summary is unavailable")
    if execute and click_mode == "image" and initial_summary is not None and (
            type(initial_summary) is not int or initial_summary < 0):
        raise ValueError("synthetic click summary is malformed")
    # OCR can temporarily miss the page summary when the cursor covers it.
    # Preserve the expected total across missing frames; only a separately
    # verified click can advance it.
    expected_summary_clicks = initial_summary
    current_ocr = initial_ocr
    current_image = Path(initial_image)
    action_count = 0
    verified_clicks = 0
    summary_states = []
    move_counts = {probe: 0 for probe in probes}
    try:
        if not execute:
            return {"state": "observed", "verified_clicks": 0, "actions": 0,
                    "evidence_path": str(root)}
        for round_number in range(1, rounds + 1):
            for probe_id in probes:
                consecutive_wakes = 0
                initial_count = counts.get(probe_id)
                if type(initial_count) is not int:
                    raise ValueError("target count is unavailable")
                for _ in range(MAX_MOVES_PER_PROBE + 1):
                    if stop_token.is_set():
                        raise RuntimeError("stopped")
                    if clock() >= deadline or action_count >= MAX_ACTIONS:
                        raise RuntimeError("trial budget reached")
                    try:
                        png = current_image.read_bytes()
                    except Exception:
                        raise RuntimeError("current private image unavailable") from None
                    plan = plan_pointer_step(probe_id, png, run_context["nonce"], current_ocr,
                                             pointer_fn=pointer_fn, wake_count=consecutive_wakes)
                    _append_jsonl(steps_path, {"stage": "plan", "round": round_number,
                        "probe_id": probe_id, "plan": plan})
                    if plan["kind"] == "stop":
                        raise RuntimeError("image planning stopped")
                    prior_target_count = counts.get(probe_id)
                    prior_summary_clicks = current_ocr.get("summary_clicks")
                    if plan["kind"] in {"move", "wake"}:
                        if move_counts[probe_id] >= MAX_MOVES_PER_PROBE:
                            raise RuntimeError("per-target movement limit reached")
                        operation = "move"
                        args = {"dx": plan["dx"], "dy": plan["dy"]}
                        if plan["kind"] == "wake":
                            consecutive_wakes += 1
                        else:
                            consecutive_wakes = 0
                        before_rows = _validate_run_events(observer.events(run_context["run_id"]), run_context)
                        if before_rows is None:
                            raise RuntimeError("page run or nonce changed")
                    else:
                        if counts.get(probe_id) != initial_count:
                            raise RuntimeError("target count changed before click")
                        if click_mode == "pointer-event":
                            pointer_seq = _latest_pointer_sequence(observer, run_context, probe_id, clock=clock)
                            if pointer_seq is None:
                                raise RuntimeError("latest trusted pointer is not on the selected probe")
                        else:
                            pointer_seq = None
                        before_value = observer.events(run_context["run_id"])
                        before_rows = _validate_run_events(before_value, run_context)
                        if before_rows is None:
                            raise RuntimeError("page run or nonce changed")
                        if click_mode == "image":
                            operation = "image-click"
                            args = {"probe_id": probe_id, "expected_count": prior_target_count}
                        else:
                            operation = "click"
                            args = {"pointer_seq": pointer_seq}
                    if stop_token.is_set():
                        raise RuntimeError("stopped")
                    action_count += 1
                    result = experiment(operation, **args, stop_token=stop_token)
                    action_dir = _archive_action(root, action_count, result)
                    _append_jsonl(steps_path, {"stage": "action", "round": round_number,
                        "probe_id": probe_id, "operation": operation, "arguments": args,
                        "state": result.get("state"), "verified": False,
                        "evidence": str(Path("actions") / action_dir.name)})
                    if result.get("state") != "executed":
                        raise RuntimeError("action outcome unknown or rejected; do not retry")
                    receipt = json.loads((action_dir / "receipt.json").read_text(encoding="utf-8"))
                    if (receipt.get("state") != "executed" or receipt.get("verified") is not False
                            or receipt.get("operation") != operation
                            or receipt.get("run_id") != run_context["run_id"]):
                        raise RuntimeError("one-shot receipt does not match current run")
                    after_image = action_dir / "after.png"
                    if not after_image.is_file():
                        raise RuntimeError("after screenshot is missing")
                    after_ocr = ocr_reader(after_image, run_context["nonce"])
                    after_row = _target_bounds(after_ocr, probe_id)
                    after_summary = after_ocr.get("summary_clicks")
                    if after_row is None:
                        raise RuntimeError("target is no longer uniquely visible")
                    if after_summary is not None and (type(after_summary) is not int or after_summary < 0):
                        raise RuntimeError("synthetic click summary is malformed")
                    if click_mode == "pointer-event" and after_summary is None:
                        raise RuntimeError("visible synthetic click summary is unavailable")
                    if operation == "move":
                        move_counts[probe_id] += 1
                        events = _validate_run_events(observer.events(run_context["run_id"]), run_context)
                        summary_changed = (after_summary != prior_summary_clicks)
                        if click_mode == "image":
                            summary_changed = (after_summary is not None
                                and expected_summary_clicks is not None
                                and after_summary != expected_summary_clicks)
                        if (events is None or not _event_delta_has_no_click(before_rows, events, run_context)
                                or after_row[0]["count"] != prior_target_count
                                or summary_changed):
                            raise RuntimeError("unexplained page change followed a movement")
                        if after_summary is not None:
                            expected_summary_clicks = after_summary
                        current_image, current_ocr = after_image, after_ocr
                        continue

                    after_rows = _validate_run_events(observer.events(run_context["run_id"]), run_context)
                    if after_rows is None or not _event_delta_is_exact_click(before_rows, after_rows,
                                                                              run_context, probe_id):
                        raise RuntimeError("independent trusted click event did not match selected probe")
                    if after_row[0]["count"] not in (initial_count, initial_count + 1):
                        raise RuntimeError("click count changed unexpectedly")
                    if (click_mode == "image" and after_summary is not None
                            and expected_summary_clicks is not None):
                        if after_summary not in (expected_summary_clicks, expected_summary_clicks + 1):
                            raise RuntimeError("synthetic click summary changed unexpectedly")
                        if (after_summary == expected_summary_clicks + 1
                                and after_ocr.get("summary_last") != probe_id):
                            raise RuntimeError("page last-click summary names a different probe")
                    delayed_count = None
                    for poll in range(click_verify_polls):
                        if stop_token.is_set() or clock() >= deadline:
                            raise RuntimeError("stopped during click verification")
                        sleep(click_verify_interval)
                        snapshot = observer.snapshot()
                        snapshot_validator(snapshot, target=run_context["target"],
                                           nonce=run_context["nonce"], clock=clock)
                        delayed_path = action_dir / f"delayed-{poll + 1:02d}.png"
                        delayed_path.write_bytes(snapshot.image_bytes)
                        (action_dir / f"delayed-{poll + 1:02d}-metadata.json").write_text(
                            json.dumps(snapshot.metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                        delayed_ocr = ocr_reader(delayed_path, run_context["nonce"])
                        delayed_row = _target_bounds(delayed_ocr, probe_id)
                        if delayed_row is None:
                            raise RuntimeError("delayed target OCR is ambiguous")
                        delayed_count = delayed_row[0]["count"]
                        delayed_summary = delayed_ocr.get("summary_clicks")
                        if delayed_summary is not None and (type(delayed_summary) is not int or delayed_summary < 0):
                            raise RuntimeError("delayed synthetic click summary is malformed")
                        if delayed_count == initial_count + 1:
                            summary_verified = None
                            baseline_summary = expected_summary_clicks
                            if delayed_summary is not None:
                                if delayed_ocr.get("summary_last") != probe_id:
                                    raise RuntimeError("page last-click summary names a different probe")
                                if baseline_summary is not None:
                                    if delayed_summary != baseline_summary + 1:
                                        raise RuntimeError("page click summary did not increase by exactly one")
                                    summary_verified = True
                            elif click_mode == "pointer-event" and delayed_summary is None:
                                raise RuntimeError("visible synthetic click summary is unavailable")
                            summary_states.append(summary_verified)
                            if delayed_summary is not None:
                                expected_summary_clicks = delayed_summary
                            elif expected_summary_clicks is not None:
                                # The target count and trusted click event
                                # independently verified the action, so carry
                                # the expected total forward even if OCR missed
                                # the summary in this delayed frame.
                                expected_summary_clicks += 1
                            current_image, current_ocr = delayed_path, delayed_ocr
                            counts[probe_id] = delayed_count
                            delayed_baseline = initial_count
                            verified_clicks += 1
                            _append_jsonl(steps_path, {"stage": "click_verified", "round": round_number,
                                "probe_id": probe_id, "before_count": delayed_baseline,
                                "after_count": delayed_count, "trusted_event": True,
                                "summary_verified": summary_verified,
                                "evidence": str(Path("actions") / action_dir.name)})
                            break
                        if delayed_count != initial_count:
                            raise RuntimeError("click count changed by an unexpected amount")
                    else:
                        raise RuntimeError("click effect was not observed; do not retry")
                    break
                else:
                    raise RuntimeError("per-target movement limit reached")
        return {"state": "completed", "verified_clicks": verified_clicks,
                "summary_verified": (True if summary_states and all(value is True for value in summary_states)
                                     else None),
                "actions": action_count, "evidence_path": str(root)}
    except BaseException:
        return {"state": "stopped", "verified_clicks": verified_clicks,
                "actions": action_count, "evidence_path": str(root), "error_code": "TRIAL_ABORTED"}


def _run_ocr(image_path, nonce, *, timeout=30):
    from phonelab.local_ocr import read_probe_ocr
    return read_probe_ocr(image_path, nonce, timeout=timeout)


def _new_trial_directory(private):
    root = private / "visual-trials"
    root.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    path = root / name
    path.mkdir(exist_ok=False)
    return path


def _parser():
    parser = argparse.ArgumentParser(description="Bounded synthetic probe-page visual trials")
    parser.add_argument("--execute", action="store_true", help="authorize the requested bounded page actions")
    parser.add_argument("--probes", nargs="+", help="distinct synthetic probe IDs, for example 01 02")
    parser.add_argument("--rounds", type=int, choices=(1, 2), default=1)
    parser.add_argument("--image-clicks", action="store_true",
                        help="use the reviewed image-guard click path; still requires --execute")
    return parser


def main(argv=None):
    cli_started = time.monotonic()
    args = _parser().parse_args(argv)
    if args.execute:
        if not args.probes:
            print(json.dumps({"state": "NOT_SENT", "error_code": "PROBES_REQUIRED"}))
            return 2
        try:
            probes = validate_trial_request(args.probes, args.rounds)
        except ValueError:
            print(json.dumps({"state": "NOT_SENT", "error_code": "INVALID_TRIAL_REQUEST"}))
            return 2
    else:
        probes = ()
    data_root = Path(deploy.DATA)
    private = _private_root(data_root / "private")
    trial_root = _new_trial_directory(private)
    token = None
    try:
        if args.execute:
            token = StopStore(private).capture()
            if token.is_set():
                raise RuntimeError("stopped")
        control_token = _load_control_token(private)
        _serial, target = read_target_binding(data_root)
        client = LiveObservationClient(BASE_URL, control_token, target)
        run_context = client.visible_run(test_id=TEST_ID, seed=SEED)
        run_context = {**run_context, "target": target}
        initial = run_experiment("observe", data_root=data_root, private_root=private,
            target_reader=lambda _root: ("", target), client_factory=lambda *_: client)
        if initial.get("state") != "observed":
            raise RuntimeError("initial observation unavailable")
        initial_receipt = json.loads((Path(initial["evidence_path"]) / "receipt.json").read_text(encoding="utf-8"))
        if initial_receipt.get("run_id") != run_context["run_id"]:
            raise RuntimeError("page run changed during initial observation")
        initial_image = Path(initial["evidence_path"]) / "screen.png"
        private_initial = trial_root / "initial.png"
        shutil.copyfile(initial_image, private_initial)
        initial_image = private_initial
        initial_ocr = _run_ocr(initial_image, run_context["nonce"])
        sanitized_ocr = {"summary_clicks": initial_ocr.get("summary_clicks"),
                         "summary_last": initial_ocr.get("summary_last"),
                         "visible_probes": [{"id": row.get("id"), "count": row.get("count"),
                             "image_text_bounds": row.get("image_text_bounds")}
                             for row in initial_ocr.get("visible_probes", []) if isinstance(row, dict)]}
        (trial_root / "initial-observation.json").write_text(json.dumps(sanitized_ocr,
            sort_keys=True, separators=(",", ":")), encoding="utf-8")
        if not args.execute:
            result = {"state": "observed", "verified_clicks": 0, "actions": 0,
                      "evidence_path": str(trial_root)}
        else:
            experiment = lambda operation, **kwargs: run_experiment(operation,
                data_root=data_root, private_root=private, target_reader=lambda _root: ("", target),
                client_factory=lambda *_: client, stop_token=kwargs.pop("stop_token"), **kwargs)
            result = run_trials(probes, args.rounds, execute=True, initial_image=initial_image,
                initial_ocr=initial_ocr, run_context=run_context, observer=client,
                experiment=experiment, ocr_reader=_run_ocr, stop_token=token,
                evidence_root=trial_root / "matrix", started_monotonic=cli_started,
                click_mode="image" if args.image_clicks else "pointer-event")
        (trial_root / "run.json").write_text(json.dumps({key: result[key] for key in
            ("state", "verified_clicks", "summary_verified", "actions", "error_code") if key in result},
            sort_keys=True, separators=(",", ":")), encoding="utf-8")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0 if result["state"] in {"observed", "completed"} else 1
    except BaseException:
        safe = {"state": "stopped", "verified_clicks": 0, "actions": 0,
                "evidence_path": str(trial_root), "error_code": "LOCAL_PREFLIGHT_FAILED"}
        try:
            (trial_root / "run.json").write_text(json.dumps(safe, sort_keys=True,
                separators=(",", ":")), encoding="utf-8")
        except Exception:
            pass
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
