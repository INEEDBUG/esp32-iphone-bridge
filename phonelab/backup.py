"""OEM preservation; no persistent-write, erase, reset-to-app, or restore operation."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path


class BackupError(RuntimeError):
    pass


def flash_capacity(jedec):
    exponent = (jedec >> 16) & 0xff
    manufacturer = jedec & 0xff
    if manufacturer in (0, 0xff) or not 18 <= exponent <= 25:
        raise BackupError('unrecognized JEDEC flash capacity; do not guess')
    return 1 << exponent


def save_new(path, data):
    with Path(path).open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def preserve(rom, directory, identity):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()): raise BackupError('baseline directory must be empty; never overwrite')
    if identity.get('identity_confirmed') is not True: raise BackupError('target identity not confirmed')
    if rom.CHIP_NAME.lower() != 'esp32': raise BackupError('classic ESP32 required')
    security = {
        'flash_encryption': bool(rom.get_flash_encryption_enabled()),
        'secure_boot': bool(rom.get_secure_boot_enabled()),
        'encrypted_download_disabled': bool(rom.get_encrypted_download_disabled()),
    }
    if any(security.values()): raise BackupError('security state needs review; no force or protection changes')
    jedec = rom.flash_id()
    size = flash_capacity(jedec)
    if hasattr(rom, 'flash_set_parameters'):
        rom.flash_set_parameters(size)  # ROM volatile parameters only; does not update image headers
    manifest = {'state': 'READING', 'time_utc': datetime.now(timezone.utc).isoformat(),
                'range': {'address': 0, 'size': size}, 'jedec_id': hex(jedec),
                'security': security, 'identity': identity, 'files': [],
                'mode': 'official esptool ROM read_flash; same connection; no stub/no post reset',
                'restore_tested': False, 'oem_state': 'PRESERVE_OEM_IN_PLACE',
                'note': 'Current flash/config snapshot; not source or proven factory state. No eFuse key backup. Same disk copies are not independent disaster backups.'}
    save_new(root / 'identification.json', json.dumps(manifest, indent=2).encode('utf-8'))
    data = []
    try:
        for index in (1, 2):
            if hasattr(rom, 'IS_STUB'):
                def progress(done, total, address):
                    update = {'read': index, 'bytes': done, 'total': total, 'address': address}
                    temporary = root / 'progress.tmp'
                    temporary.write_text(json.dumps(update), encoding='utf-8')
                    os.replace(temporary, root / 'progress.json')
                raw = rom.read_flash(0, size, progress)
            else:
                raw = rom.read_flash(0, size)
            if not isinstance(raw, bytes): raise BackupError('ROM did not return bytes')
            filename = f'read-{index}.bin'
            save_new(root / filename, raw)
            manifest['files'].append({'name': filename, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
            if len(raw) != size: raise BackupError('short full-flash read retained; baseline invalid')
            data.append(raw)
        if data[0] != data[1]:
            manifest['state'] = 'MISMATCH'
            raise BackupError('independent reads differ; keep originals and stop')
        manifest['state'] = 'DOUBLE_READ_MATCH'
        for item in manifest['files']:
            (root / item['name']).chmod(0o444)
        return manifest
    except Exception:
        if manifest['state'] == 'READING': manifest['state'] = 'FAILED'
        raise
    finally:
        save_new(root / 'manifest.json', json.dumps(manifest, indent=2).encode('utf-8'))


def read_confirmed_board(port, directory, expected_instance):
    """Only called after human/PnP binding. No probing any other serial port."""
    import subprocess
    destination = Path(directory)
    if destination.exists() and any(destination.iterdir()):
        raise BackupError('baseline already contains evidence; no port was opened')
    from serial.tools import list_ports
    if not expected_instance or not expected_instance.startswith('USB\\VID_10C4&PID_EA60\\'):
        raise BackupError('confirmed CP210x instance required')
    matches = [p for p in list_ports.comports() if p.device == port and p.vid == 0x10c4 and p.pid == 0xea60]
    if len(matches) != 1: raise BackupError('selected port does not match the bound CP210x')
    # Compare the complete live Windows instance, not only the generic USB bridge serial number.
    query = 'Get-CimInstance Win32_SerialPort | Select-Object DeviceID,PNPDeviceID | ConvertTo-Json -Compress'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', query], capture_output=True, text=True, check=True)
    rows = json.loads(result.stdout or '[]')
    if isinstance(rows, dict): rows = [rows]
    if not any(row['DeviceID'] == port and row['PNPDeviceID'].upper() == expected_instance.upper() for row in rows):
        raise BackupError('Windows PnP identity changed')
    import esptool
    from esptool.targets.esp32 import ESP32ROM
    from esptool.loader import ESPLoader
    if esptool.__version__ != '5.3.1': raise BackupError('review required for changed esptool version')
    # Opening UART may affect DTR/RTS. No speculative opens/logging before this explicit ROM operation.
    rom = None
    try:
        rom = ESP32ROM(port=port, baud=115200)
        rom.connect(mode='default-reset', attempts=2, detecting=True)
        # The general detector first tries get-security-info, unsupported on classic ESP32.
        # Verify the official ROM magic directly rather than assuming the selected chip class.
        magic = rom.read_reg(ESPLoader.CHIP_DETECT_MAGIC_REG_ADDR)
        if magic != ESP32ROM.MAGIC_VALUE: raise BackupError('ROM magic is not classic ESP32')
        if rom.get_flash_encryption_enabled() or rom.get_secure_boot_enabled() or rom.get_encrypted_download_disabled():
            raise BackupError('security state requires review before flash access')
        # Official ROM SPI attach using actual eFuse pad config; deliberately avoid generic
        # attach_flash's optional XMC startup/reset side paths. No guessed board GPIO mapping.
        clk, q, d, hd, cs = rom.get_chip_spi_pads()
        rom.flash_spi_attach((hd << 24) | (cs << 18) | (d << 12) | (q << 6) | clk)
        manifest = preserve(rom, directory, {'identity_confirmed': True, 'pnp_instance': expected_instance,
                                            'port_at_read': port, 'esptool_version': esptool.__version__})
        return manifest
    finally:
        # Close only; never hard_reset(), run(), write_flash(), or erase. Remains in ROM download state.
        if rom is not None: rom._port.close()


def restore_preview(manifest_path):
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest['state'] != 'DOUBLE_READ_MATCH': raise BackupError('backup not valid')
    files = manifest.get('files', [])
    if len(files) != 2 or {item.get('name') for item in files} != {'read-1.bin', 'read-2.bin'}:
        raise BackupError('exactly two original independent read files are required')
    images = []
    for file in files:
        image_path = (path.parent / file['name']).resolve()
        if not image_path.is_relative_to(path.parent.resolve()): raise BackupError('image path escapes baseline')
        raw = image_path.read_bytes()
        if len(raw) != manifest['range']['size'] or hashlib.sha256(raw).hexdigest() != file['sha256']:
            raise BackupError('backup integrity failure')
        images.append(raw)
    if images[0] != images[1]: raise BackupError('independent read images differ')
    return {'write_allowed': False, 'restore_tested': False, 'address': 0,
            'image': str(path.parent / manifest['files'][0]['name']),
            'parameters': 'full raw image at address 0; flash header settings keep; security/target recheck required',
            'approval': 'Separate explicit restore authorization required; this function never executes writes.'}
