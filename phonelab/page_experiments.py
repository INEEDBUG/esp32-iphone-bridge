"""Explicit one-shot experiments limited to the currently visible synthetic page.

This module is deliberately separate from PhoneLab startup and generic control.
It never returns an unlocked state or treats a device ACK as verified effect.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time

from phonelab import deploy, runtime
from phonelab.iphone import read_target_binding
from phonelab.live_observation import BASE_URL, LiveObservationClient
from phonelab.page_scope import (PageMovementExperiment, PageObservedClickExperiment,
                                 PageObservedScrollExperiment, PageImageLocatedClickExperiment,
                                 PageImageLocatedScrollExperiment, windows_input_lease)
from phonelab.visual_marker import decode_visual_marker
from phonelab.stop_signal import StopStore


ROOT = Path(__file__).resolve().parents[1]
TEST_ID = "ble-round-01"
SEED = "42"
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")


def _private_root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute():
        raise ValueError("private evidence root must be absolute")
    resolved = root.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("private evidence must remain outside the source repository")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise ValueError("private evidence must remain outside Git repositories")
    return resolved


def _load_control_token(private: Path) -> str:
    try:
        config = json.loads((private / "runtime.json").read_text(encoding="utf-8"))
        token = config["token"]
    except Exception:
        raise ValueError("local PhoneLab runtime token is unavailable") from None
    if not isinstance(token, str) or not 20 <= len(token) <= 256:
        raise ValueError("local PhoneLab runtime token is invalid")
    return token


def _validate_request(operation, *, dx=None, dy=None, pointer_seq=None, vertical=None,
                      probe_id=None, expected_count=None):
    if operation == "observe":
        if any(value is not None for value in (dx, dy, pointer_seq, vertical, probe_id, expected_count)):
            raise ValueError("observe accepts no action arguments")
        return {}
    if operation == "move":
        if (type(dx) is not int or type(dy) is not int or
                not -16 <= dx <= 16 or not -16 <= dy <= 16 or (dx, dy) == (0, 0)):
            raise ValueError("move requires nonzero integer deltas within 16 per axis")
        return {"dx": dx, "dy": dy}
    if operation == "click":
        if type(pointer_seq) is not int or pointer_seq < 1:
            raise ValueError("click requires the current positive pointer event sequence")
        return {"pointer_seq": pointer_seq}
    if operation in {'image-click', 'image-scroll'}:
        if (not isinstance(probe_id, str) or probe_id not in {f'{n:02d}' for n in range(1, 26)}
                or type(expected_count) is not int or not 0 <= expected_count <= 999999
                or any(value is not None for value in (dx, dy, pointer_seq))):
            raise ValueError('one synthetic image probe id and expected count are required')
        if operation == 'image-click':
            if vertical is not None:
                raise ValueError('image-click accepts no wheel delta')
            return {'probe_id': probe_id, 'expected_count': expected_count}
        if type(vertical) is not int or vertical == 0 or not -3 <= vertical <= 3:
            raise ValueError('image-scroll requires a vertical delta within 1..3')
        return {'probe_id': probe_id, 'expected_count': expected_count, 'vertical': vertical}
    if operation == "scroll":
        if (type(pointer_seq) is not int or pointer_seq < 1 or type(vertical) is not int
                or vertical == 0 or not -3 <= vertical <= 3):
            raise ValueError("scroll requires a current pointer sequence and vertical delta within 3")
        return {"pointer_seq": pointer_seq, "vertical": vertical}
    raise ValueError("unsupported page experiment")


def _validate_snapshot_structure(snapshot, *, target, nonce):
    meta = getattr(snapshot, "metadata", None)
    if (not isinstance(meta, dict) or meta.get("target") != target
            or meta.get("source") != "USB_SCREENSHOT" or meta.get("orientation") != "portrait"
            or meta.get("fresh") is not True or meta.get("state") != "fresh"
            or meta.get("locked") != "UNKNOWN" or (meta.get("width"), meta.get("height")) != (828, 1792)
            or meta.get("source_time") is not None
            or type(meta.get("sequence")) is not int or meta["sequence"] < 1
            or type(meta.get("received_monotonic")) not in (int, float)
            or not math.isfinite(meta["received_monotonic"])):
        raise ValueError("bound USB screen observation is invalid")
    decode_visual_marker(snapshot.image_bytes, nonce)
    return snapshot


def _validate_snapshot(snapshot, *, target, nonce, clock):
    _validate_snapshot_structure(snapshot, target=target, nonce=nonce)
    received = snapshot.metadata["received_monotonic"]
    if not 0 <= clock() - received <= 2:
        raise ValueError("fresh bound USB screen observation is unavailable")
    return snapshot


def _new_evidence_dir(private_root, *, now=None):
    root = _private_root(private_root) / "onsite-input"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / timestamp
    path.mkdir(exist_ok=False)
    return path


def _write_bytes(path, payload):
    with Path(path).open("xb") as stream:
        stream.write(payload)
        stream.flush()


def _save_snapshot(directory, name, snapshot, *, target, nonce, clock):
    if name not in {"screen", "before", "after", "page-association"}:
        raise ValueError("invalid evidence image name")
    # Archiving preserves evidence that was validated when observed. It does not
    # reauthorize that frame for an input decision or renew its freshness.
    _validate_snapshot_structure(snapshot, target=target, nonce=nonce)
    _write_bytes(directory / f"{name}.png", snapshot.image_bytes)
    record = {"metadata": snapshot.metadata, "age_seconds": getattr(snapshot, "age_seconds", None),
              "age_at_save_seconds": max(0.0, clock() - snapshot.metadata["received_monotonic"])}
    (directory / f"{name}-metadata.json").write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _safe_events(observer, run_id):
    try:
        value = observer.events(run_id)
        if not isinstance(value, dict) or not isinstance(value.get("events"), list):
            return {"events": []}
        rows = value["events"]
        return {"run": value.get("run"), "events": rows[-128:], "truncated": len(rows) > 128}
    except Exception:
        return {"events": [], "read_error": "OBSERVATION_UNAVAILABLE"}


class _RecordingObserver:
    """Tag only fresh PNGs containing this run's marker; never persist other pages."""
    def __init__(self, observer, *, target, nonce, clock, dispatch_state):
        self._observer = observer
        self._target, self._nonce = target, nonce
        self._clock, self._dispatch_state = clock, dispatch_state
        self.validated = []

    def visible_run(self, **kwargs):
        return self._observer.visible_run(**kwargs)

    def events(self, run_id):
        return self._observer.events(run_id)

    def snapshot(self):
        snapshot = self._observer.snapshot()
        try:
            _validate_snapshot(snapshot, target=self._target, nonce=self._nonce, clock=self._clock)
        except Exception:
            return snapshot
        self.validated.append(("after" if self._dispatch_state["started"] else "before", snapshot))
        return snapshot


class _TrackedBackend:
    def __init__(self, backend, dispatch_state):
        self._backend, self._dispatch_state = backend, dispatch_state

    def execute(self, request):
        self._dispatch_state["started"] = True
        return self._backend.execute(request)

    def close(self):
        return self._backend.close()


def run_experiment(operation, *, dx=None, dy=None, pointer_seq=None, vertical=None,
                   probe_id=None, expected_count=None,
                   test_id=TEST_ID, seed=SEED, data_root=deploy.DATA, private_root=None,
                   target_reader=read_target_binding, client_factory=None,
                   backend_opener=None, lease_factory=None, clock=None, sleep=None,
                   stop_token=None):
    """Run one explicit read or synthetic-page action; return sanitized local evidence status."""
    arguments = _validate_request(operation, dx=dx, dy=dy, pointer_seq=pointer_seq,
                                  vertical=vertical, probe_id=probe_id, expected_count=expected_count)
    if (test_id, seed) != (TEST_ID, SEED):
        raise ValueError("only the approved synthetic test id and seed are supported")
    data_root = Path(data_root)
    private = _private_root(Path(private_root) if private_root else data_root / "private")
    # Capture before any page, identity or UART preparation. A batch must pass
    # the same token to every one-shot; it must never recapture after a stop.
    token = None if operation == 'observe' else (
        stop_token if stop_token is not None else StopStore(private).capture())
    dispatch_state = {"started": False}
    evidence = None
    observer = None
    recording = None
    first_snapshot = None
    target = None
    run_id = None
    nonce = None
    try:
        if token is not None and token.is_set():
            raise ValueError('experiment generation stopped')
        control_token = _load_control_token(private)
        _serial, target = target_reader(data_root)
        if client_factory is None:
            client_factory = lambda control_token, label: LiveObservationClient(
                BASE_URL, control_token, label, clock=clock)
        observer = client_factory(control_token, target)
        run = observer.visible_run(test_id=test_id, seed=seed)
        if (not isinstance(run, dict) or run.get("test_id") != test_id or run.get("seed") != seed
                or run.get("phone_verified") is not False or not isinstance(run.get("nonce"), str)
                or not isinstance(run.get("run_id"), str) or not _RUN_ID.fullmatch(run["run_id"])):
            raise ValueError("visible synthetic page run failed validation")
        run_id, nonce = run["run_id"], run["nonce"]
        now_fn = clock or time.monotonic
        linked = observer.events(run_id)
        linked_run = linked.get("run") if isinstance(linked, dict) else None
        if (not isinstance(linked_run, dict) or linked_run.get("run_id") != run_id
                or linked_run.get("nonce") != nonce or linked_run.get("test_id") != test_id
                or linked_run.get("seed") != seed or linked_run.get("phone_verified") is not False):
            raise ValueError("visible synthetic page session changed")
        first_snapshot = _validate_snapshot(observer.snapshot(), target=target, nonce=nonce, clock=now_fn)
        evidence = _new_evidence_dir(private)

        if operation == "observe":
            _save_snapshot(evidence, "screen", first_snapshot, target=target, nonce=nonce, clock=now_fn)
            event_data = _safe_events(observer, run_id)
            (evidence / "events.json").write_text(
                json.dumps(event_data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            receipt = {"state": "observed", "verified": False,
                       "interaction_scope": "current_synthetic_page_only", "lock_state": "UNKNOWN",
                       "test_id": test_id, "seed": seed, "run_id": run_id,
                       "image": "screen.png", "events": "events.json"}
            (evidence / "receipt.json").write_text(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            return {"state": "observed", "verified": False, "evidence_id": evidence.name,
                    "evidence_path": str(evidence)}

        _save_snapshot(evidence, "page-association", first_snapshot,
                       target=target, nonce=nonce, clock=now_fn)
        recording = _RecordingObserver(observer, target=target, nonce=nonce,
                                       clock=now_fn, dispatch_state=dispatch_state)
        def tracked_open():
            if token.is_set():
                raise ValueError('experiment generation stopped')
            backend = backend_opener() if backend_opener else runtime.open_approved(
                data=data_root, private=private, image_root=ROOT,
                epoch_reader=runtime.read_windows_connection_epoch, stop_token=token)
            return _TrackedBackend(backend, dispatch_state)
        lease = lease_factory or (lambda: windows_input_lease(private / "input-owner.lock"))
        common = {"target": target, "run_id": run_id, "nonce": nonce, "test_id": test_id,
                  "backend_opener": tracked_open, "lease_factory": lease,
                  "cancel_token": token,
                  **({"clock": clock} if clock else {}), **({"sleep": sleep} if sleep else {})}
        if operation == "move":
            experiment = PageMovementExperiment(recording, **common)
            result = experiment.move(arguments["dx"], arguments["dy"])
        elif operation == "click":
            experiment = PageObservedClickExperiment(recording, **common)
            result = experiment.click(arguments["pointer_seq"])
        elif operation in {'image-click', 'image-scroll'}:
            from phonelab.image_probe_guard import ImageProbeGuard
            from phonelab.local_ocr import read_probe_ocr
            image = evidence / 'page-association.png'
            ocr = read_probe_ocr(image, nonce)
            selected = [v for v in ocr['visible_probes'] if isinstance(v, dict)
                        and v.get('id') == arguments['probe_id']]
            if len(selected) != 1 or selected[0].get('count') != arguments['expected_count']:
                raise ValueError('planned probe count does not match current image; do not replay')
            guard = ImageProbeGuard(arguments['probe_id'], first_snapshot.image_bytes, nonce, ocr)
            if operation == 'image-click':
                experiment = PageImageLocatedClickExperiment(recording, image_guard=guard,
                    expected_count=arguments['expected_count'], probe_id=arguments['probe_id'], **common)
                result = experiment.click()
            else:
                experiment = PageImageLocatedScrollExperiment(recording, image_guard=guard,
                    expected_count=arguments['expected_count'], probe_id=arguments['probe_id'], **common)
                result = experiment.scroll(arguments['vertical'])
        elif operation == "scroll":
            experiment = PageObservedScrollExperiment(recording, **common)
            result = experiment.scroll(arguments["pointer_seq"], arguments["vertical"])

        # The scope object returned only after its fresh before/after checks.
        _save_snapshot(evidence, "before", result["before"], target=target, nonce=nonce, clock=now_fn)
        _save_snapshot(evidence, "after", result["after"], target=target, nonce=nonce, clock=now_fn)
        event_data = _safe_events(observer, run_id)
        (evidence / "events.json").write_text(
            json.dumps(event_data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        receipt = {"state": "executed", "verified": False,
                   "interaction_scope": "current_synthetic_page_only", "lock_state": "UNKNOWN",
                   "test_id": test_id, "seed": seed, "run_id": run_id,
                   "operation": operation, "arguments": arguments,
                   "request_id": result.get("request_id"), "events": "events.json",
                   "association": "page-association.png",
                   "before": "before.png", "after": "after.png"}
        (evidence / "receipt.json").write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return {"state": "executed", "verified": False, "evidence_id": evidence.name,
                "evidence_path": str(evidence), "operation": operation}
    except BaseException as exc:
        state = "UNKNOWN_OR_NOT_SENT" if dispatch_state["started"] else "NOT_SENT"
        if evidence is not None:
            if recording is not None:
                for stage in ("before", "after"):
                    if (evidence / f"{stage}.png").exists():
                        continue
                    candidates = [snapshot for label, snapshot in recording.validated if label == stage]
                    fallback = candidates[-1] if candidates else (first_snapshot if stage == "before" else None)
                    if fallback is not None:
                        try:
                            _save_snapshot(evidence, stage, fallback, target=target, nonce=nonce,
                                           clock=clock or time.monotonic)
                        except Exception:
                            pass
            elif first_snapshot is not None and not (evidence / "before.png").exists():
                try:
                    _save_snapshot(evidence, "before", first_snapshot, target=target, nonce=nonce,
                                   clock=clock or time.monotonic)
                except Exception:
                    pass
            try:
                events = _safe_events(observer, run_id) if run_id else {"events": []}
                (evidence / "events.json").write_text(
                    json.dumps(events, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            except Exception:
                pass
            receipt = {"state": state, "verified": False,
                       "interaction_scope": "current_synthetic_page_only", "lock_state": "UNKNOWN",
                       "operation": operation, "error_type": type(exc).__name__,
                       "error_code": "PAGE_EXPERIMENT_FAILED", "dispatch_started": dispatch_state["started"],
                       "events": "events.json"}
            try:
                (evidence / "receipt.json").write_text(
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            except Exception:
                pass
        return {"state": state, "verified": False,
                "evidence_id": evidence.name if evidence else None,
                "evidence_path": str(evidence) if evidence else None,
                "error_type": type(exc).__name__, "error_code": "PAGE_EXPERIMENT_FAILED"}


def _parser():
    parser = argparse.ArgumentParser(description="One-shot synthetic-page BLE experiment; no general input")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("observe", "move", "click", "image-click", "scroll", "image-scroll"):
        command = sub.add_parser(name)
        command.add_argument("--test-id", choices=(TEST_ID,), default=TEST_ID)
        command.add_argument("--seed", choices=(SEED,), default=SEED)
        if name == "move":
            command.add_argument("--dx", type=int, required=True)
            command.add_argument("--dy", type=int, required=True)
        elif name == "click":
            command.add_argument("--pointer-seq", type=int, required=True)
        elif name in {'image-click', 'image-scroll'}:
            command.add_argument('--probe-id', choices=tuple(f'{n:02d}' for n in range(1, 26)), required=True)
            command.add_argument('--expected-count', type=int, required=True)
            if name == 'image-scroll':
                command.add_argument('--vertical', type=int, required=True)
        elif name == "scroll":
            command.add_argument("--pointer-seq", type=int, required=True)
            command.add_argument("--vertical", type=int, required=True)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    test_id, seed = args.test_id, args.seed
    values = vars(args)
    operation = values.pop("operation")
    values.pop("test_id", None)
    values.pop("seed", None)
    try:
        result = run_experiment(operation, test_id=test_id, seed=seed, **values)
    except BaseException as exc:
        result = {"state": "NOT_SENT", "verified": False,
                  "error_type": type(exc).__name__, "error_code": "LOCAL_PREFLIGHT_FAILED"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["state"] in {"observed", "executed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
