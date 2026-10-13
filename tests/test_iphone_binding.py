import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from phonelab.iphone import inspect_phone, wait_for_bound_usb


SERIAL = '0000000000000000000000000000000000000001'
OTHER = '0000000000000000000000000000000000000002'


def binding(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    record = {'udid': SERIAL, 'target': 'iphone-' + hashlib.sha256(SERIAL.encode()).hexdigest()[:12]}
    path = private / 'target.json'
    path.write_text(json.dumps(record))
    return path, record


class Lockdown:
    paired = True
    def __init__(self, version='17.5.1'):
        self.details = dict(ProductType='iPhone12,1', ProductVersion=version, BuildVersion='21F90')
        self.closed = False
    async def get_value(self, *, key): return self.details[key]
    async def close(self): self.closed = True


def mocks(monkeypatch, devices, lockdown=None):
    from pymobiledevice3 import usbmux
    from pymobiledevice3 import lockdown as module
    listing = AsyncMock(return_value=devices)
    opener = AsyncMock(return_value=lockdown)
    monkeypatch.setattr(usbmux, 'list_devices', listing)
    monkeypatch.setattr(module, 'create_using_usbmux', opener)
    return listing, opener


def test_other_connected_phone_never_replaces_binding_or_receives_service_command(tmp_path, monkeypatch):
    path, record = binding(tmp_path)
    before = path.read_bytes()
    _, opener = mocks(monkeypatch, [SimpleNamespace(is_usb=True, serial=OTHER)])
    result = asyncio.run(inspect_phone(tmp_path))
    assert result['DEVICE_IDENTIFIED'] == 'UNKNOWN'
    assert result['target'] == record['target']
    opener.assert_not_awaited()
    assert path.read_bytes() == before


def test_bound_target_is_selected_with_other_phone_present_and_default_does_not_probe_ports(tmp_path, monkeypatch):
    path, _ = binding(tmp_path)
    connect = AsyncMock(side_effect=AssertionError('default inspection must not probe service ports'))
    current = SimpleNamespace(is_usb=True, serial=SERIAL, connect=connect)
    device = Lockdown()
    _, opener = mocks(monkeypatch, [SimpleNamespace(is_usb=True, serial=OTHER), current], device)
    before = path.read_bytes()
    result = asyncio.run(inspect_phone(tmp_path))
    assert result['DEVICE_IDENTIFIED'] == result['PAIRING_VALID'] == 'VERIFIED'
    assert opener.await_args.kwargs['serial'] == SERIAL
    assert opener.await_args.kwargs['autopair'] is False
    assert opener.await_args.kwargs['connection_type'] == 'USB'
    connect.assert_not_awaited()
    assert device.closed and path.read_bytes() == before


def test_missing_binding_rejects_before_usbmux_or_pairing_request(tmp_path, monkeypatch):
    listing, opener = mocks(monkeypatch, [])
    result = asyncio.run(inspect_phone(tmp_path))
    assert result['DEVICE_IDENTIFIED'] == 'UNKNOWN'
    listing.assert_not_awaited()
    opener.assert_not_awaited()
    assert not (tmp_path / 'private/target.json').exists()


def test_changed_frozen_os_metadata_is_not_accepted_or_rebound(tmp_path, monkeypatch):
    path, _ = binding(tmp_path)
    before = path.read_bytes()
    phone = Lockdown(version='other-version')
    mocks(monkeypatch, [SimpleNamespace(is_usb=True, serial=SERIAL)], phone)
    result = asyncio.run(inspect_phone(tmp_path))
    assert result['DEVICE_IDENTIFIED'] == 'FAILED'
    assert phone.closed and path.read_bytes() == before


def test_wait_only_enumerates_until_exact_usb_target_arrives(tmp_path):
    binding(tmp_path)
    wrong = SimpleNamespace(is_usb=True, serial=OTHER)
    network = SimpleNamespace(is_usb=False, serial=SERIAL)
    target = SimpleNamespace(is_usb=True, serial=SERIAL)
    listing = AsyncMock(side_effect=[[wrong], [network], [wrong, target]])
    sleep = AsyncMock()
    asyncio.run(wait_for_bound_usb(tmp_path, list_devices_fn=listing, sleep_fn=sleep))
    assert listing.await_count == 3 and sleep.await_count == 2


def test_wait_cancellation_and_duplicate_usb_identity_fail_without_rebinding(tmp_path):
    path, _ = binding(tmp_path)
    before = path.read_bytes()
    listing = AsyncMock(return_value=[])
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(wait_for_bound_usb(tmp_path, list_devices_fn=listing, sleep_fn=sleep))
    duplicate = SimpleNamespace(is_usb=True, serial=SERIAL)
    with pytest.raises(ValueError):
        asyncio.run(wait_for_bound_usb(tmp_path, list_devices_fn=AsyncMock(return_value=[duplicate, duplicate])))
    assert path.read_bytes() == before
