import hashlib
import json
import time
from pathlib import Path

import pytest

from phonelab import deploy, runtime


EPOCH = {
    "DEVPKEY_Device_LastArrivalDate": "2026-10-13T06:00:00.0000000Z",
    "DEVPKEY_Device_ContainerId": "{12345678-1234-5678-9abc-1234567890ab}",
}
IDENTITY = {"DeviceID": "COM9", "PNPDeviceID": "USB\\VID_10C4&PID_EA60\\TEST"}


def fixture(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    image = tmp_path / "image.bin"
    image.write_bytes(b"F" * deploy.SIZE)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    monkeypatch.setattr(deploy, "FW_SHA", digest)
    (private / "deploy-approval.json").write_text(json.dumps({
        "approved": True, "experimental_sha256": digest,
    }))
    (private / "board-connection-epoch.json").write_text(json.dumps(EPOCH))
    rows = [
        {"attempt": 1, "state": "started", "experimental_sha256": digest,
         "oem_sha256": deploy.OEM_SHA},
        {"attempt": 1, "state": "verified", "experimental_sha256": digest,
         "oem_sha256": deploy.OEM_SHA, "readback_sha256": digest,
         "readback_size": deploy.SIZE},
        {"attempt": 1, "state": "booted_experimental", "experimental_sha256": digest,
         "oem_sha256": deploy.OEM_SHA, "readback_sha256": digest,
         "readback_size": deploy.SIZE},
    ]
    (private / "deploy-receipts.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    def preflight(data, private_arg, image_root):
        return {"image": image, "identity": IDENTITY}
    def port_finder(identity):
        assert identity == IDENTITY
        return "COM9"
    return private, image, digest, preflight, port_finder


def test_readiness_requires_receipt_epoch_and_current_hash_without_open(tmp_path, monkeypatch):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    result = runtime.build_readiness(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
                                     port_finder=port_finder, preflight_fn=preflight)
    assert result["ready"] is True
    assert result["receipt_continuity_only"] is True
    assert result["board_attestation"] is False


def test_changed_usb_arrival_blocks_readiness(tmp_path, monkeypatch):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    changed = {**EPOCH, "DEVPKEY_Device_LastArrivalDate": "different"}
    result = runtime.build_readiness(data=tmp_path, private=private, epoch_reader=lambda _: changed,
                                     port_finder=port_finder, preflight_fn=preflight)
    assert result["ready"] is False
    assert result["reasons"] == ["usb_connection_epoch_changed_or_unavailable"]


def test_epoch_normalizes_timezone_and_guid_case_but_rejects_unspecified_time(tmp_path, monkeypatch):
    private, _, _, _, _ = fixture(tmp_path, monkeypatch)
    live = {"DEVPKEY_Device_LastArrivalDate": "2026-10-13T14:00:00+08:00",
            "DEVPKEY_Device_ContainerId": "12345678-1234-5678-9ABC-1234567890AB"}
    assert runtime._epoch_matches(private, IDENTITY, lambda _: live)
    live["DEVPKEY_Device_LastArrivalDate"] = "2026-10-13T06:00:00"
    assert not runtime._epoch_matches(private, IDENTITY, lambda _: live)
    live["DEVPKEY_Device_LastArrivalDate"] = "2026-10-13T06:00:00Z"
    live["DEVPKEY_Device_ContainerId"] = "12345678-1234-5678-9ABC-1234567890AB}"
    assert not runtime._epoch_matches(private, IDENTITY, lambda _: live)


def test_windows_epoch_reader_passes_pnp_by_environment_and_returns_structured_json(tmp_path):
    pwsh = tmp_path / "pwsh.exe"
    pwsh.write_bytes(b"fake executable marker")
    output = json.dumps(EPOCH)
    seen = {}
    class Completed:
        stdout = output
    def runner(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return Completed()
    result = runtime.read_windows_connection_epoch(IDENTITY, pwsh_path=pwsh, runner=runner)
    assert result == EPOCH
    assert "-Command" in seen["args"]
    assert IDENTITY["PNPDeviceID"] not in " ".join(seen["args"])
    assert seen["kwargs"]["env"]["PHONELAB_PNP_DEVICE_ID"] == IDENTITY["PNPDeviceID"]
    assert "Get-PnpDeviceProperty" in seen["args"][-1]


def test_missing_epoch_or_unbooted_latest_attempt_fails_closed(tmp_path, monkeypatch):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    (private / "board-connection-epoch.json").unlink()
    result = runtime.build_readiness(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
                                     port_finder=port_finder, preflight_fn=preflight)
    assert result["ready"] is False
    assert "usb_connection_epoch_changed_or_unavailable" in result["reasons"]
    (private / "board-connection-epoch.json").write_text(json.dumps(EPOCH))
    with (private / "deploy-receipts.jsonl").open("a") as f:
        f.write(json.dumps({"attempt": 2, "state": "started"}) + "\n")
    result = runtime.build_readiness(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
                                     port_finder=port_finder, preflight_fn=preflight)
    assert "latest_deployment_not_verified_and_booted" in result["reasons"]


def test_probe_sends_only_random_session_ping_and_closes_without_stop(tmp_path, monkeypatch):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    class Wire:
        def __init__(self): self.writes = []; self.lines = []; self.closed = False
        def write(self, payload):
            self.writes.append(payload)
            req = json.loads(payload)
            self.lines.append(json.dumps({"id": req["id"], "session": req["session"],
                "state": "rejected", "error": "session_mismatch", "ble_connected": False}).encode() + b"\n")
        def readline(self, size=513): return self.lines.pop(0) if self.lines else b""
        def close(self): self.closed = True
    wire = Wire()
    result = runtime.probe_unarmed_status(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
        port_finder=port_finder, preflight_fn=preflight, wire_factory=lambda _: wire)
    request = json.loads(wire.writes[0])
    assert result["state"] == "unarmed" and result["ble_connected"] is False
    assert request["op"] == "ping" and request["session"] != ""
    assert "hello" not in [json.loads(item)["op"] for item in wire.writes]
    assert len(wire.writes) == 1 and wire.closed


def test_session_mismatch_is_unarmed_even_if_ble_status_is_connected(tmp_path, monkeypatch):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    class Wire:
        def write(self, payload):
            req = json.loads(payload)
            self.line = json.dumps({"id": req["id"], "session": req["session"],
                "state": "rejected", "error": "session_mismatch", "ble_connected": True,
                "mouse_ready": False, "mouse_send_failures": 2}).encode() + b"\n"
        def readline(self, size=513):
            line, self.line = getattr(self, "line", b""), b""
            return line
        def close(self): pass
    result = runtime.probe_unarmed_status(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
        port_finder=port_finder, preflight_fn=preflight, wire_factory=lambda _: Wire())
    assert result["state"] == "unarmed" and result["ble_connected"] is True
    assert result['mouse_ready'] is False and result['mouse_send_failures'] == 2


def test_probe_gate_failure_never_constructs_wire(tmp_path, monkeypatch):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="serial open refused"):
        runtime.probe_unarmed_status(data=tmp_path, private=private, epoch_reader=lambda _: {},
            port_finder=port_finder, preflight_fn=preflight,
            wire_factory=lambda _: pytest.fail("must not open"))


@pytest.mark.parametrize('malformed', [False, True])
def test_probe_distinguishes_link_from_report_readiness_without_raw_diagnostics(tmp_path, monkeypatch, malformed):
    private, _, _, preflight, port_finder = fixture(tmp_path, monkeypatch)
    fields = {'ble_link_connected': True, 'keyboard_ready': False, 'advertising': False,
              'disconnect_count': 3, 'last_disconnect_reason': 19, 'advertising_restart_failed': False}
    if malformed: fields = {key: 'PRIVATE' for key in fields}
    class Wire:
        def write(self, payload):
            req = json.loads(payload)
            self.line = json.dumps({'id': req['id'], 'session': req['session'],
                'state': 'rejected', 'error': 'session_mismatch',
                'ble_diag': {'link': fields['ble_link_connected'], 'adv': fields['advertising'],
                    'dc': fields['disconnect_count'], 'dr': fields['last_disconnect_reason'],
                    'af': fields['advertising_restart_failed'], 'peerAddress': 'must-not-retain'},
                'ble_connected': fields['keyboard_ready']}).encode() + b'\n'
        def readline(self, size=513):
            line, self.line = getattr(self, 'line', b''), b''
            return line
        def close(self): pass
    result = runtime.probe_unarmed_status(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
        port_finder=port_finder, preflight_fn=preflight, wire_factory=lambda _: Wire())
    assert result['state'] == 'unarmed'
    assert {key: result[key] for key in fields} == ({key: None for key in fields} if malformed else fields)
    assert 'peerAddress' not in result and 'PRIVATE' not in json.dumps(result)


def test_explicit_open_passes_only_bound_approval(monkeypatch, tmp_path):
    private, _, digest, preflight, port_finder = fixture(tmp_path, monkeypatch)
    captured = {}
    class FakeBackend:
        @classmethod
        def open_approved(cls, port, approval, *, stop_token):
            assert not stop_token.is_set()
            captured.update(port=port, approval=approval)
            return "opened"
    monkeypatch.setattr("phonelab.serial_backend.SerialBackend", FakeBackend)
    assert runtime.open_approved(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
        port_finder=port_finder, preflight_fn=preflight) == "opened"
    assert captured == {"port": "COM9", "approval": {
        "experimental_firmware_written_with_permission": True,
        "deployed_firmware_sha256": digest, "port": "COM9"}}


def test_stop_during_approved_preflight_never_opens_uart(tmp_path, monkeypatch):
    from phonelab.stop_signal import StopStore
    private, _, digest, preflight, port_finder = fixture(tmp_path, monkeypatch)
    store = StopStore(private)
    token = store.capture()
    def stopped_preflight(*args):
        result = preflight(*args)
        store.publish_stop()
        return result
    class NeverOpen:
        @classmethod
        def open_approved(cls, *args, **kwargs): pytest.fail("stopped preparation must not open UART")
    monkeypatch.setattr("phonelab.serial_backend.SerialBackend", NeverOpen)
    with pytest.raises(RuntimeError, match="generation_stopped"):
        runtime.open_approved(data=tmp_path, private=private, epoch_reader=lambda _: EPOCH,
            port_finder=port_finder, preflight_fn=stopped_preflight, stop_token=token)
