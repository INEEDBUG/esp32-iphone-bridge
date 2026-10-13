"""Explicit runtime readiness and unarmed status probe for the deployed BLE bridge.

Receipts prove local workflow continuity only; they are not cryptographic board attestation.
No function here is wired into normal startup, and readiness inspection never opens UART.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from phonelab import deploy
from phonelab.paths import resolve_pwsh

PWSH_PATH = resolve_pwsh()
_EPOCH_PS = r"""
$ErrorActionPreference = 'Stop'
$instanceId = $env:PHONELAB_PNP_DEVICE_ID
if ([string]::IsNullOrWhiteSpace($instanceId)) { throw 'missing PnP instance' }
$props = Get-PnpDeviceProperty -InstanceId $instanceId -KeyName @('DEVPKEY_Device_LastArrivalDate','DEVPKEY_Device_ContainerId')
$map = @{}
foreach ($property in $props) { $map[$property.KeyName] = $property.Data }
$arrival = $map['DEVPKEY_Device_LastArrivalDate']
if ($arrival -is [DateTime]) {
  if ($arrival.Kind -eq [DateTimeKind]::Utc -or $arrival.Kind -eq [DateTimeKind]::Local) {
    $arrival = $arrival.ToUniversalTime().ToString('o')
  } else { $arrival = $arrival.ToString('o') }
} elseif ($arrival -is [DateTimeOffset]) { $arrival = $arrival.ToUniversalTime().ToString('o') }
[ordered]@{ DEVPKEY_Device_LastArrivalDate = $arrival; DEVPKEY_Device_ContainerId = [string]$map['DEVPKEY_Device_ContainerId'] } | ConvertTo-Json -Compress
"""


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _receipt_ready(private: Path, current_sha: str) -> bool:
    path = private / "deploy-receipts.jsonl"
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows = [r for r in rows if isinstance(r, dict) and type(r.get("attempt")) is int]
    except (OSError, ValueError):
        return False
    if not rows:
        return False
    latest = max(r["attempt"] for r in rows)
    current = [r for r in rows if r["attempt"] == latest]
    states = [r.get("state") for r in current]
    if not all(s in states for s in ("started", "verified", "booted_experimental")):
        return False
    start = next((r for r in current if r.get("state") == "started"), None)
    verified = next((r for r in reversed(current) if r.get("state") == "verified"), None)
    booted = current[-1] if current[-1].get("state") == "booted_experimental" else None
    return bool(start and verified and booted and
                start.get("experimental_sha256") == current_sha == deploy.FW_SHA and
                start.get("oem_sha256") == deploy.OEM_SHA and
                verified.get("readback_sha256") == current_sha and
                verified.get("readback_size") == deploy.SIZE and
                booted.get("readback_sha256") == current_sha and
                booted.get("oem_sha256") == deploy.OEM_SHA)


def _epoch_matches(private: Path, identity: dict, epoch_reader) -> bool:
    recorded = _read_json(private / "board-connection-epoch.json")
    if recorded is None or not callable(epoch_reader):
        return False
    arrival_key = "DEVPKEY_Device_LastArrivalDate"
    container_key = "DEVPKEY_Device_ContainerId"
    expected_arrival = recorded.get(arrival_key)
    expected_container = recorded.get(container_key)
    if not isinstance(expected_arrival, str) or not expected_arrival or not isinstance(expected_container, str) or not expected_container:
        return False
    try:
        live = epoch_reader(identity)
    except Exception:
        return False
    if not isinstance(live, dict):
        return False
    expected_date = _normalize_datetime(expected_arrival)
    live_date = _normalize_datetime(live.get(arrival_key))
    expected_guid = _normalize_guid(expected_container)
    live_guid = _normalize_guid(live.get(container_key))
    return bool(expected_date and live_date and expected_guid and live_guid and
                expected_date == live_date and expected_guid == live_guid)


def _normalize_datetime(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None  # Never guess the machine timezone for an unspecified datetime.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _normalize_guid(value):
    if not isinstance(value, str) or not value:
        return None
    pattern = r"(?:[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}|\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\})"
    if re.fullmatch(pattern, value) is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def read_windows_connection_epoch(identity: dict, *, pwsh_path: Path = PWSH_PATH,
                                  runner=subprocess.run) -> dict:
    """Read exact Windows arrival/container properties for the bound PnP instance."""
    instance = identity.get("PNPDeviceID") if isinstance(identity, dict) else None
    if not isinstance(instance, str) or not instance or not pwsh_path.is_file():
        raise RuntimeError("bound device epoch unavailable")
    env = dict(os.environ)
    env["PHONELAB_PNP_DEVICE_ID"] = instance
    completed = runner([str(pwsh_path), "-NoProfile", "-NonInteractive", "-Command", _EPOCH_PS],
                       capture_output=True, text=True, check=True, env=env)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("device epoch response invalid")
    return value


def build_readiness(*, data: Path = deploy.DATA, private: Path | None = None,
                    image_root: Path = deploy.ROOT, epoch_reader=None,
                    port_finder=deploy._find_port, preflight_fn=deploy.preflight) -> dict:
    """Read-only readiness inspection. Does not open the serial port."""
    private = private or data / "private"
    reasons = []
    try:
        checked = preflight_fn(data, private, image_root)
    except Exception:
        return {"ready": False, "reasons": ["deployment_preflight_failed"],
                "receipt_continuity_only": True, "board_attestation": False}
    image = checked.get("image")
    try:
        current_sha = deploy.sha256(Path(image))
    except Exception:
        current_sha = ""
    approval = _read_json(private / "deploy-approval.json") or {}
    if not current_sha or approval.get("approved") is not True or approval.get("experimental_sha256") != current_sha:
        reasons.append("approved_image_mismatch")
    if not current_sha or not _receipt_ready(private, current_sha):
        reasons.append("latest_deployment_not_verified_and_booted")
    identity = checked.get("identity")
    if not isinstance(identity, dict):
        reasons.append("board_identity_missing")
    else:
        try:
            port = port_finder(identity)
            if port != identity.get("DeviceID"):
                reasons.append("live_serial_identity_mismatch")
        except Exception:
            reasons.append("live_serial_identity_unavailable")
        if not _epoch_matches(private, identity, epoch_reader):
            reasons.append("usb_connection_epoch_changed_or_unavailable")
    return {"ready": not reasons, "reasons": reasons,
            "receipt_continuity_only": True, "board_attestation": False}


def _approved_open_context(*, data, private, image_root, epoch_reader, port_finder, preflight_fn):
    """Repeat every gate immediately before an explicit open; reject stale readiness results."""
    result = build_readiness(data=data, private=private, image_root=image_root,
                             epoch_reader=epoch_reader, port_finder=port_finder,
                             preflight_fn=preflight_fn)
    if not result["ready"]:
        raise RuntimeError("serial open refused: " + ",".join(result["reasons"]))
    checked = preflight_fn(data, private, image_root)
    port = port_finder(checked["identity"])
    if port != checked["identity"].get("DeviceID") or not _epoch_matches(private, checked["identity"], epoch_reader):
        raise RuntimeError("serial open refused: usb_connection_epoch_changed_or_unavailable")
    approval = {"experimental_firmware_written_with_permission": True,
                "deployed_firmware_sha256": deploy.sha256(Path(checked["image"])),
                "port": port}
    return port, approval


def open_approved(*, data: Path = deploy.DATA, private: Path | None = None,
                  image_root: Path = deploy.ROOT, epoch_reader,
                  port_finder=deploy._find_port, preflight_fn=deploy.preflight,
                  stop_token=None):
    """Explicitly open an approved SerialBackend after current identity/epoch rechecks."""
    from phonelab.serial_backend import SerialBackend
    from phonelab.stop_signal import StopStore
    private = private or data / "private"
    token = stop_token if stop_token is not None else StopStore(private).capture()
    if token.is_set():
        raise RuntimeError('serial open refused: generation_stopped')
    port, approval = _approved_open_context(data=data, private=private, image_root=image_root,
                                            epoch_reader=epoch_reader, port_finder=port_finder,
                                            preflight_fn=preflight_fn)
    if token.is_set():
        raise RuntimeError('serial open refused: generation_stopped')
    backend = SerialBackend.open_approved(port, approval, stop_token=token)
    if token.is_set():
        backend.close()
        raise RuntimeError('serial open refused: generation_stopped')
    return backend


def probe_unarmed_status(*, data: Path = deploy.DATA, private: Path | None = None,
                         image_root: Path = deploy.ROOT, epoch_reader,
                         port_finder=deploy._find_port, preflight_fn=deploy.preflight,
                         wire_factory=None, timeout: float = 2.0) -> dict:
    """Send one random-session ping (no hello/HID action), then close without stop."""
    if not 0 < timeout <= 2.0:
        raise ValueError("status probe timeout must be at most two seconds")
    private = private or data / "private"
    port, _ = _approved_open_context(data=data, private=private, image_root=image_root,
                                     epoch_reader=epoch_reader, port_finder=port_finder,
                                     preflight_fn=preflight_fn)
    try:
        if wire_factory:
            wire = wire_factory(port)
        else:
            import serial
            wire = serial.Serial(port=None, baudrate=115200, timeout=.05, write_timeout=.3)
            wire.dtr = False
            wire.rts = False
            wire.port = port
            wire.open()
    except Exception:
        return {"state": "probe_unavailable", "ble_connected": None,
                "receipt_continuity_only": True, "board_attestation": False}
    request_id = "probe-" + uuid.uuid4().hex
    session = secrets.token_hex(24)
    request = {"id": request_id, "session": session, "op": "ping", "args": {},
               "ttl_ms": min(1800, max(1, int(timeout * 900)))}
    raw = json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n"
    result = {"state": "timeout", "ble_connected": None,
              "receipt_continuity_only": True, "board_attestation": False}
    deadline = time.monotonic() + timeout
    pending = b""
    try:
        wire.write(raw)
        while time.monotonic() < deadline:
            chunk = wire.readline(513)
            pending += chunk
            if len(pending) > 512:
                pending = b""
                continue
            if b"\n" not in pending:
                if not chunk:
                    time.sleep(.001)
                continue
            line, _, pending = pending.partition(b"\n")
            try:
                reply = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(reply, dict) or reply.get("id") != request_id or reply.get("session") != session:
                continue
            connected = reply.get("ble_connected") if isinstance(reply.get("ble_connected"), bool) else None
            result['mouse_ready'] = reply.get('mouse_ready') if type(reply.get('mouse_ready')) is bool else None
            failures = reply.get('mouse_send_failures')
            result['mouse_send_failures'] = failures if type(failures) is int and 0 <= failures <= 0xffffffff else None
            if reply.get("state") == "rejected" and reply.get("error") == "session_mismatch":
                result.update(state="unarmed", ble_connected=connected)
            elif reply.get("state") == "executed":
                result.update(state="unexpected_executed", ble_connected=connected)
            else:
                result.update(state="protocol_rejected", ble_connected=connected)
            break
        return result
    finally:
        # This probe never hello-arms a session; do not send stop or any HID command on close.
        wire.close()
