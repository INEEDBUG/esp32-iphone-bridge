import hashlib
import json
from pathlib import Path

import pytest

from phonelab import deploy


def make_tree(tmp_path):
    data = tmp_path / "data"
    private = data / "private"
    private.mkdir(parents=True)
    image = data / "firmware" / "build" / "classic_ble"
    image.mkdir(parents=True)
    fw = b"F" * deploy.SIZE
    (image / "classic_ble.ino.merged.bin").write_bytes(fw)
    backup = data / "oem-backups" / "baseline-20261013-01"
    backup.mkdir(parents=True)
    oem = b"O" * deploy.SIZE
    for n in (1, 2):
        (backup / f"read-{n}.bin").write_bytes(oem)
    (backup / "manifest.json").write_text(json.dumps({
        "state": "DOUBLE_READ_MATCH", "range": {"address": 0, "size": deploy.SIZE},
        "restore_tested": False, "security": {"flash_encryption": False, "secure_boot": False,
        "encrypted_download_disabled": False}, "jedec_id": "0x164046",
        "files": [{"name": f"read-{n}.bin", "size": deploy.SIZE,
                   "sha256": hashlib.sha256(oem).hexdigest()} for n in (1, 2)]}))
    protected = data / "oem-backups" / "protected-copy"
    protected.mkdir()
    (protected / "first-read-baseline-20261013.bin").write_bytes(oem)
    (private / "board-identity.json").write_text(json.dumps({"DeviceID": "COM9", "PNPDeviceID": "USB\\VID_10C4&PID_EA60\\TEST"}))
    (private / "chip-identification.json").write_text(json.dumps({"revision": 301,
        "jedec": "0x164046", "chip": "ESP32-D0WD-V3 (revision v3.1)"}))
    (private / "deploy-approval.json").write_text(json.dumps({"approved": True,
        "max_experimental_writes": 3, "experimental_sha256": deploy.FW_SHA,
        "oem_sha256": deploy.OEM_SHA}))
    # Use artifact contents matching pinned digests while keeping the test independent of huge fixture data.
    # Tests below use intentionally bad pinned bytes to prove rejection before any port callback.
    return data, private


def test_requires_explicit_approval_before_any_port(tmp_path):
    data, private = make_tree(tmp_path)
    (private / "deploy-approval.json").unlink()
    called = []
    with pytest.raises(deploy.DeployError, match="metadata"):
        deploy.execute(data=data, private=private, image_root=data, port_finder=lambda _: called.append(True))
    assert called == []


def test_complete_preflight_accepts_pinned_test_fixture(tmp_path, monkeypatch):
    data, private = make_tree(tmp_path)
    fw_path = data / "firmware" / "build" / "classic_ble" / "classic_ble.ino.merged.bin"
    oem_path = data / "oem-backups" / "baseline-20261013-01" / "read-1.bin"
    fw_hash = hashlib.sha256(fw_path.read_bytes()).hexdigest()
    oem_hash = hashlib.sha256(oem_path.read_bytes()).hexdigest()
    monkeypatch.setattr(deploy, "FW_SHA", fw_hash)
    monkeypatch.setattr(deploy, "OEM_SHA", oem_hash)
    approval = json.loads((private / "deploy-approval.json").read_text())
    approval.update(experimental_sha256=fw_hash, oem_sha256=oem_hash)
    (private / "deploy-approval.json").write_text(json.dumps(approval))
    manifest_path = data / "oem-backups" / "baseline-20261013-01" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        item["sha256"] = oem_hash
    manifest_path.write_text(json.dumps(manifest))
    result = deploy.preflight(data, private, image_root=data)
    assert result["identity_ok"] and result["oem_ok"]
    assert result["image"] == fw_path


def test_hash_mismatch_stops_before_any_port(tmp_path):
    data, private = make_tree(tmp_path)
    called = []
    with pytest.raises(deploy.DeployError, match="experimental image size/hash"):
        deploy.execute(data=data, private=private, image_root=data, port_finder=lambda _: called.append(True))
    assert called == []
    assert not (private / "deploy.lock").exists()


def test_approval_must_pin_both_exact_hashes(tmp_path):
    data, private = make_tree(tmp_path)
    approval = json.loads((private / "deploy-approval.json").read_text())
    approval["oem_sha256"] = "0" * 64
    (private / "deploy-approval.json").write_text(json.dumps(approval))
    with pytest.raises(deploy.DeployError, match="approval hashes"):
        deploy.preflight(data, private)


def test_lock_and_attempt_limit_fail_closed(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    fd = deploy._acquire_lock(private / "deploy.lock")
    try:
        with pytest.raises(deploy.DeployError, match="lock exists"):
            deploy._acquire_lock(private / "deploy.lock")
    finally:
        import os
        os.close(fd)
    for i in range(1, 4):
        (private / f"attempt{i:02d}").mkdir()
    with pytest.raises(deploy.DeployError, match="limit reached"):
        deploy._next_attempt(private)


def test_no_execute_cli_never_invokes_deployment(monkeypatch, capsys):
    monkeypatch.setattr(deploy, "execute", lambda **_: pytest.fail("must not execute"))
    assert deploy.main([]) == 2
    assert "No action taken" in capsys.readouterr().out


def test_changed_connection_after_serial_open_never_reaches_writer(tmp_path, monkeypatch):
    from types import SimpleNamespace
    private = tmp_path / 'private'; private.mkdir()
    (private / 'deploy-approval.json').write_text('{}')
    image = tmp_path / 'image.bin'; image.write_bytes(b'fixture')
    identity = {'DeviceID': 'COM9', 'PNPDeviceID': 'fictional-instance'}
    monkeypatch.setattr(deploy, 'preflight', lambda *args: {'image': image, 'identity': identity})
    opened = SimpleNamespace(port='COM9', is_open=True)
    opened.close = lambda: setattr(opened, 'is_open', False)
    rom = SimpleNamespace(_port=opened, connect=lambda **kwargs: None)
    checks = iter((True, False))
    written = []
    with pytest.raises(deploy.DeployError):
        deploy.execute(data=tmp_path, private=private, port_finder=lambda _: 'COM9',
                       rom_factory=lambda _: rom, writer=lambda *args: written.append(True),
                       continuity_checker=lambda *args: next(checks))
    assert written == [] and opened.is_open is False
    assert json.loads((private/'attempt01/failure.log').read_text())['error_type'] == 'DeployError'
    assert not (private/'deploy.lock').exists()


def test_prewrite_identity_check_rejects_epoch_port_or_closed_handle(tmp_path):
    from types import SimpleNamespace
    identity = {'DeviceID': 'COM9'}
    rom = SimpleNamespace(_port=SimpleNamespace(port='COM9', is_open=True))
    assert deploy._require_open_identity(rom, 'COM9', identity, tmp_path, lambda _: 'COM9', lambda *args: True) is None
    for handle_port, opened, found_port, same_epoch in [
        ('COM8', True, 'COM9', True), ('COM9', False, 'COM9', True),
        ('COM9', True, 'COM8', True), ('COM9', True, 'COM9', False),
    ]:
        rom._port = SimpleNamespace(port=handle_port, is_open=opened)
        with pytest.raises(deploy.DeployError):
            deploy._require_open_identity(rom, 'COM9', identity, tmp_path, lambda _: found_port,
                                          lambda *args: same_epoch)


def test_epoch_change_immediately_before_write_stops_and_sanitizes_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from esptool.targets.esp32 import ESP32ROM
    private = tmp_path / 'private'; private.mkdir()
    (private/'deploy-approval.json').write_text('{}')
    image = tmp_path/'image.bin'; image.write_bytes(b'fixture')
    identity = {'DeviceID': 'COM9', 'PNPDeviceID': 'fictional-instance'}
    monkeypatch.setattr(deploy, 'preflight', lambda *args: {'image': image, 'identity': identity})
    handle = SimpleNamespace(port='COM9', is_open=True)
    handle.close = lambda: setattr(handle, 'is_open', False)
    rom = SimpleNamespace(_port=handle, connect=lambda **kwargs: None,
        read_reg=lambda _: ESP32ROM.MAGIC_VALUE, get_flash_encryption_enabled=lambda: False,
        get_secure_boot_enabled=lambda: False, get_encrypted_download_disabled=lambda: False,
        get_chip_spi_pads=lambda: (0,0,0,0,0), flash_spi_attach=lambda _: None,
        flash_id=lambda: 0x164046, get_chip_revision=lambda: 301,
        flash_set_parameters=lambda _: None, change_baud=lambda _: None)
    rom.run_stub = lambda: rom
    checks = []
    def continuity(*args):
        checks.append(True)
        if len(checks) == 3:
            raise RuntimeError('SYNTHETIC_PRIVATE_FAILURE_VALUE')
        return True
    written = []
    with pytest.raises(deploy.DeployError):
        deploy.execute(data=tmp_path, private=private, port_finder=lambda _: 'COM9',
                       rom_factory=lambda _: rom, writer=lambda *args: written.append(True),
                       continuity_checker=continuity)
    assert len(checks) == 3 and written == [] and handle.is_open is False
    failure = (private/'attempt01/failure.log').read_text()
    assert 'SYNTHETIC_PRIVATE_FAILURE_VALUE' not in failure
    assert json.loads(failure)['error_type'] == 'RuntimeError'
