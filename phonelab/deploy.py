"""Explicit, bounded first-write helper. Never imported by normal PhoneLab startup."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from phonelab.backup import restore_preview
from phonelab.paths import PROJECT_ROOT, resolve_data_root, resolve_pwsh

ROOT = PROJECT_ROOT
DATA = resolve_data_root()
PRIVATE = DATA / "private"
OEM = DATA / "oem-backups" / "baseline-20261013-01"
SIZE = 4 * 1024 * 1024
FW_SHA = "9ccfea8f6c5e89c8fee9f07b8a444c2b3784becd098026d78aaab3a51bb3ae7e"
OEM_SHA = "e6a2a9492d20b17e29f0e517f449577a718c9fec1d09a507c5833869818ac8a5"
ESPT_VER = "5.3.1"


class DeployError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception as exc:
        raise DeployError("required private metadata is missing or invalid") from exc


def _write_limit(approval: dict) -> int:
    limit = approval.get("max_experimental_writes")
    if approval.get("approved") is not True or type(limit) is not int or limit not in (3, 4):
        raise DeployError("explicit deployment approval absent or out of scope")
    if limit == 4:
        extra = approval.get("additional_write")
        if (not isinstance(extra, dict) or extra.get("approved") is not True
                or extra.get("previous_limit") != 3 or extra.get("new_limit") != 4
                or extra.get("experimental_sha256") != FW_SHA
                or extra.get("range") != {"address": 0, "size": SIZE}):
            raise DeployError("exact fourth-write approval is required")
    return limit


def preflight(data: Path = DATA, private: Path | None = None, image_root: Path = ROOT) -> dict:
    """All disk/approval checks. Safe for tests; opens no port and writes nothing."""
    private = private or data / "private"
    approval = _json(private / "deploy-approval.json")
    write_limit = _write_limit(approval)
    if approval.get("experimental_sha256") != FW_SHA or approval.get("oem_sha256") != OEM_SHA:
        raise DeployError("approval hashes do not match pinned artifacts")
    image = image_root / "firmware" / "build" / "classic_ble" / "classic_ble.ino.merged.bin"
    if not image.is_file() or image.stat().st_size != SIZE or sha256(image) != FW_SHA:
        raise DeployError("experimental image size/hash mismatch")
    backup = data / "oem-backups" / "baseline-20261013-01"
    manifest = _json(backup / "manifest.json")
    if manifest.get("state") != "DOUBLE_READ_MATCH" or manifest.get("range") != {"address": 0, "size": SIZE}:
        raise DeployError("OEM recovery image is not a verified full-flash double read")
    if manifest.get("restore_tested") is not False or manifest.get("security") != {
        "flash_encryption": False, "secure_boot": False, "encrypted_download_disabled": False
    } or manifest.get("jedec_id") != "0x164046":
        raise DeployError("OEM target recovery/security evidence changed")
    preview = restore_preview(backup / "manifest.json")
    if preview.get("write_allowed") is not False or preview.get("restore_tested") is not False or preview.get("address") != 0:
        raise DeployError("OEM restore preview did not validate as a full raw image")
    listed = {x.get("name"): x for x in manifest.get("files", []) if isinstance(x, dict)}
    if set(listed) != {"read-1.bin", "read-2.bin"}:
        raise DeployError("both original OEM reads are required")
    for name in ("read-1.bin", "read-2.bin"):
        path = backup / name
        if not path.is_file() or path.stat().st_size != SIZE or sha256(path) != OEM_SHA:
            raise DeployError("OEM source integrity mismatch")
        if listed[name].get("size") != SIZE or listed[name].get("sha256") != OEM_SHA:
            raise DeployError("OEM manifest integrity mismatch")
    if (backup / "read-1.bin").read_bytes() != (backup / "read-2.bin").read_bytes():
        raise DeployError("OEM independent reads differ")
    # The specifically designated, third same-disk copy is part of the recovery set.
    protected = data / "oem-backups" / "protected-copy" / "first-read-baseline-20261013.bin"
    if not protected.is_file() or protected.stat().st_size != SIZE or sha256(protected) != OEM_SHA:
        raise DeployError("protected OEM copy missing or does not match")
    identity = _json(private / "board-identity.json")
    if not isinstance(identity.get("DeviceID"), str) or not isinstance(identity.get("PNPDeviceID"), str):
        raise DeployError("bound board identity missing")
    chip = _json(private / "chip-identification.json")
    if chip.get("revision") != 301 or chip.get("jedec") != "0x164046" or "v3.1" not in chip.get("chip", ""):
        raise DeployError("previous ROM chip identification differs")
    return {"image": image, "identity": identity, "attempt_root": private,
            "identity_ok": True, "oem_ok": True, "write_limit": write_limit}


def _acquire_lock(path: Path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii")); os.fsync(fd)
        return fd
    except FileExistsError as exc:
        raise DeployError("deployment lock exists; no automatic stale-lock recovery") from exc


def _next_attempt(private: Path, *, limit: int = 3) -> tuple[int, Path]:
    if type(limit) is not int or limit not in (3, 4):
        raise DeployError("deployment attempt limit is out of scope")
    found = []
    for path in private.glob("attempt*"):
        m = re.fullmatch(r"attempt(\d{2})", path.name)
        if m:
            found.append(int(m.group(1)))
    ledger = private / "deploy-receipts.jsonl"
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                n = int(json.loads(line)["attempt"])
                found.append(n)
            except Exception as exc:
                raise DeployError("receipt ledger is invalid; refusing a new write") from exc
    n = max(found, default=0) + 1
    if n > limit:
        raise DeployError("authorized deployment attempt limit reached")
    path = private / f"attempt{n:02d}"
    path.mkdir()
    return n, path


def _receipt(private: Path, record: dict):
    path = private / "deploy-receipts.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n"); f.flush(); os.fsync(f.fileno())


def _find_port(identity: dict):
    from serial.tools import list_ports
    expected_device = identity["DeviceID"]
    expected_pnp = identity["PNPDeviceID"].upper()
    matches = [p for p in list_ports.comports() if p.device == expected_device and p.vid == 0x10C4 and p.pid == 0xEA60]
    if len(matches) != 1:
        raise DeployError("selected CP210x serial identity is unavailable or ambiguous")
    cmd = "Get-CimInstance Win32_SerialPort | Select-Object DeviceID,PNPDeviceID | ConvertTo-Json -Compress"
    result = subprocess.run([str(resolve_pwsh()), "-NoProfile", "-NonInteractive", "-Command", cmd],
                            capture_output=True, text=True, check=True)
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict): rows = [rows]
    if not any(r.get("DeviceID") == expected_device and str(r.get("PNPDeviceID", "")).upper() == expected_pnp for r in rows):
        raise DeployError("Windows PnP identity changed")
    return expected_device


def _connection_continuity(private: Path, identity: dict) -> bool:
    # Lazy import: this shares the read-only Windows epoch check with the
    # runtime gate and never opens a serial port or starts an input backend.
    from phonelab.runtime import _epoch_matches, read_windows_connection_epoch
    return _epoch_matches(private, identity, read_windows_connection_epoch)


def _require_open_identity(rom, port, identity, private, port_finder, continuity_checker):
    handle = getattr(rom, '_port', None)
    if (handle is None or getattr(handle, 'is_open', None) is not True or
            getattr(handle, 'port', None) != port or port_finder(identity) != port or
            continuity_checker(private, identity) is not True):
        raise DeployError('opened serial handle or USB connection identity changed')


def execute(*, boot=False, data: Path = DATA, private: Path | None = None,
            image_root: Path = ROOT, port_finder=_find_port, rom_factory=None, writer=None,
            continuity_checker=_connection_continuity):
    private = private or data / "private"
    checked = preflight(data, private, image_root)  # critical: before lock/port
    approval = _json(private / "deploy-approval.json")
    lock_path = private / "deploy.lock"
    lockfd = _acquire_lock(lock_path)
    attempt_dir = None
    rom = None
    try:
        # Recheck under exclusive lock so another invocation cannot race approval/artifacts.
        checked = preflight(data, private, image_root)
        if continuity_checker(private, checked['identity']) is not True:
            raise DeployError('USB connection epoch changed or is unavailable; re-identification required')
        attempt, attempt_dir = _next_attempt(private, limit=checked.get("write_limit", 3))
        record = {"attempt": attempt, "state": "started", "time_utc": datetime.now(timezone.utc).isoformat(),
                  "experimental_sha256": FW_SHA, "oem_sha256": OEM_SHA, "boot_requested": bool(boot)}
        _receipt(private, record)  # attempt consumed before any serial connection
        port = port_finder(checked["identity"])
        import esptool
        from esptool.targets.esp32 import ESP32ROM
        from esptool.loader import ESPLoader
        from esptool.cmds import write_flash
        if esptool.__version__ != ESPT_VER:
            raise DeployError("pinned esptool version mismatch")
        rom = rom_factory(port) if rom_factory else ESP32ROM(port=port, baud=115200)
        rom.connect(mode="no-reset", attempts=2, detecting=True)
        _require_open_identity(rom, port, checked['identity'], private, port_finder, continuity_checker)
        if rom.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR) != ESP32ROM.MAGIC_VALUE:
            raise DeployError("classic ESP32 ROM magic mismatch")
        if rom.get_flash_encryption_enabled() or rom.get_secure_boot_enabled() or rom.get_encrypted_download_disabled():
            raise DeployError("chip security state is not all false")
        clk, q, d, hd, cs = rom.get_chip_spi_pads()
        rom.flash_spi_attach((hd << 24) | (cs << 18) | (d << 12) | (q << 6) | clk)
        if rom.flash_id() != 0x164046:
            raise DeployError("live JEDEC ID mismatch")
        if rom.get_chip_revision() != 301:
            raise DeployError("live chip revision mismatch")
        # Supported official stub only after all identity, ROM, security and flash gates.
        flasher = rom.run_stub()
        flasher.flash_set_parameters(SIZE)
        flasher.change_baud(460800)
        # Keep the same OS serial handle. A removed handle is never reopened or
        # rebound to a newly enumerated compatible board by this writer.
        _require_open_identity(rom, port, checked['identity'], private, port_finder, continuity_checker)
        if writer:
            writer(flasher, checked["image"])
        else:
            write_flash(flasher, [(0, str(checked["image"]))], flash_mode="keep", flash_freq="keep", flash_size="keep")
        readback = flasher.read_flash(0, SIZE)
        readback_path = attempt_dir / "readback.bin"
        with readback_path.open("xb") as f:
            f.write(readback); f.flush(); os.fsync(f.fileno())
        read_hash = hashlib.sha256(readback).hexdigest()
        if len(readback) != SIZE or read_hash != FW_SHA or readback != checked["image"].read_bytes():
            raise DeployError("full-flash same-session readback mismatch; board left in ROM/stub")
        record.update(state="verified", readback_sha256=read_hash, readback_size=len(readback))
        _receipt(private, record)
        if boot:
            flasher.hard_reset()
            record.update(state="booted_experimental")
            _receipt(private, record)
        return record
    except Exception as exc:
        record = {"attempt": locals().get("attempt", 0), "state": "failed", "time_utc": datetime.now(timezone.utc).isoformat(),
                  "error_type": type(exc).__name__, "boot_requested": bool(boot)}
        if attempt_dir is not None:
            _receipt(private, record)
            try:
                with (attempt_dir / "failure.log").open("xb") as log:
                    # Device/library exception strings can contain identifiers
                    # or packet bytes. Keep only coarse, non-secret evidence.
                    log.write(json.dumps(record, sort_keys=True).encode('utf-8'))
                    log.flush(); os.fsync(log.fileno())
            except OSError:
                pass
        raise DeployError(f"deployment failed ({type(exc).__name__}); inspect private attempt receipt") from None
    finally:
        if rom is not None:
            try: rom._port.close()
            except Exception: pass
        os.close(lockfd)
        try: lock_path.unlink()
        except OSError: pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit experimental ESP32 deployment")
    parser.add_argument("--execute", action="store_true", help="required; otherwise no serial port is opened")
    parser.add_argument("--boot", action="store_true", help="reset into experimental image after verified full readback")
    args = parser.parse_args(argv)
    if not args.execute:
        print("No action taken. --execute and private deploy approval are required.")
        return 2
    try:
        result = execute(boot=args.boot)
        print(json.dumps({k: v for k, v in result.items() if k != "time_utc"}, sort_keys=True))
        return 0
    except DeployError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
