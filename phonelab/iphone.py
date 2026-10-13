"""Targeted USB capability inspection. Never pairs, mounts DDI, installs, or reboots."""
import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
import re
from pathlib import Path


def read_target_binding(data_root):
    try:
        record = json.loads((Path(data_root) / 'private/target.json').read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError('private target binding is unavailable') from None
    if (not isinstance(record, dict) or set(record) != {'udid', 'target'}
            or not isinstance(record['udid'], str) or not re.fullmatch(r'[0-9a-fA-F-]{8,64}', record['udid'])
            or not isinstance(record['target'], str) or not re.fullmatch(r'iphone-[0-9a-f]{12}', record['target'])
            or record['target'] != 'iphone-' + hashlib.sha256(record['udid'].encode('ascii')).hexdigest()[:12]):
        raise ValueError('private target binding is invalid')
    return record['udid'], record['target']


async def wait_for_bound_usb(data_root, *, list_devices_fn=None, sleep_fn=asyncio.sleep):
    """Cancellable arrival wait; enumeration only, no pairing, services or rebinding."""
    serial, _ = read_target_binding(data_root)
    if list_devices_fn is None:
        from pymobiledevice3 import usbmux
        list_devices_fn = usbmux.list_devices
    while True:
        devices = await list_devices_fn()
        matches = [d for d in devices if d.is_usb and d.serial == serial]
        if len(matches) > 1:
            raise ValueError('confirmed USB target identity is ambiguous')
        if matches:
            return
        await sleep_fn(.5)


async def inspect_phone(data_root, capture=False, *, probe_services=False):
    from pymobiledevice3 import usbmux
    from pymobiledevice3.lockdown import create_using_usbmux
    root = Path(data_root) / 'private'
    state = {key: 'UNKNOWN' for key in ('DEVICE_IDENTIFIED','PAIRING_VALID','JAILBREAK_ACTIVE',
             'AUTHENTICATED_SHELL','FRESH_SCREEN_AVAILABLE','INPUT_AVAILABLE','RECONNECT_VERIFIED')}
    state['time_utc'] = datetime.now(timezone.utc).isoformat()
    try:
        serial, label = read_target_binding(data_root)
    except ValueError:
        state['blocker'] = 'confirmed target binding required; no device command issued'
        return state
    state['target'] = label
    devices = [d for d in await usbmux.list_devices() if d.is_usb]
    state['usb_device_count'] = len(devices)
    matches = [d for d in devices if d.serial == serial]
    if len(matches) != 1:
        state['blocker'] = 'confirmed USB target absent or ambiguous; no device command issued'
        return state
    device = matches[0]
    lockdown = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB',
                                       pairing_records_cache_folder=root / 'pairing-cache')
    try:
        details = {k: await lockdown.get_value(key=k) for k in ('ProductType','ProductVersion','BuildVersion')}
        state.update(details)
        if tuple(details[k] for k in ('ProductType','ProductVersion','BuildVersion')) != ('iPhone12,1','17.5.1','21F90'):
            state['DEVICE_IDENTIFIED'] = 'FAILED'
            state['blocker'] = 'frozen target metadata mismatch; no further capability probes'
            return state
        state['DEVICE_IDENTIFIED'] = 'VERIFIED'
        state['PAIRING_VALID'] = 'VERIFIED' if lockdown.paired else 'FAILED'
        if not lockdown.paired:
            state['blocker'] = 'existing Apple pairing invalid; no new pair attempted'
            return state
        for name, port in ([('ssh',22), ('zxtouch',6000), ('vnc',5900)] if probe_services else []):
            sock = None
            try:
                sock = await asyncio.wait_for(device.connect(port), 3)
                state[name + '_usb_port'] = 'OPEN'
                if name == 'ssh':
                    # Banner only; no username/password guess, no authentication or host-key bypass.
                    sock.settimeout(2)
                    banner = await asyncio.to_thread(sock.recv, 160)
                    state['ssh_banner_protocol'] = 'SSH' if banner.startswith(b'SSH-') else 'UNKNOWN'
            except Exception as exc:
                state[name + '_usb_port'] = 'UNAVAILABLE:' + type(exc).__name__
            finally:
                if sock is not None: sock.close()
        if capture:
            try:
                from pymobiledevice3.services.screenshot import ScreenshotService
                from PIL import Image
                async with ScreenshotService(lockdown) as service:
                    raw = await asyncio.wait_for(service.take_screenshot(), 8)
                picture = Image.open(io.BytesIO(raw))
                (root / 'screen-probe.raw').write_bytes(raw)
                state['capture_probe'] = {'service': 'com.apple.mobile.screenshotr', 'width': picture.width,
                                          'height': picture.height, 'source_time': None,
                                          'freshness_verified': False, 'private_file': 'private/screen-probe.raw'}
            except Exception as exc:
                state['capture_probe'] = {'result': 'FAILED', 'error_type': type(exc).__name__,
                                          'ddi_mounted_by_lab': False}
        return state
    finally:
        await lockdown.close()


def inspect_sync(data_root, capture=False):
    try:
        return asyncio.run(asyncio.wait_for(inspect_phone(data_root, capture), 35))
    except Exception as exc:
        return {'DEVICE_IDENTIFIED': 'UNKNOWN', 'PAIRING_VALID': 'UNKNOWN',
                'blocker': type(exc).__name__, 'new_pair_attempted': False}
