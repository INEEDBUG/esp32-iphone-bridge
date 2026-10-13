import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import usb_tunnel_patch as patch


FIXTURE = '''from functools import partial
from typing import Any, Callable, Optional, Protocol, TypeVar, cast

async def _create_no_root_tunnel_provider(serial: Optional[str], autopair: bool, remotepairing_fallback: bool = True):
    lockdown = await create_using_usbmux(serial=serial, autopair=autopair)
    return lockdown

class UserspaceRsdTunnel:
    def __init__(
        self, serial: Optional[str] = None, autopair: bool = True, remotepairing_fallback: bool = True
    ) -> None:
        self.serial = serial
        self.autopair = autopair
        self.remotepairing_fallback = remotepairing_fallback

    async def _aopen_locked(self):
            provider, lockdown = await _create_no_root_tunnel_provider(
                self.serial, self.autopair, self.remotepairing_fallback
            )
            return provider, lockdown
'''


def test_fixture_patch_threads_usb_and_private_pairing_cache_end_to_end():
    result = patch.transform(FIXTURE)
    compile(result, "userspace_tunnel.py", "exec")
    assert "from pathlib import Path" in result
    assert "connection_type: Optional[str] = None" in result
    assert "pairing_records_cache_folder: Optional[Path] = None" in result
    assert "connection_type=connection_type" in result
    assert "pairing_records_cache_folder=pairing_records_cache_folder" in result
    assert "self.connection_type = connection_type" in result
    assert "self.pairing_records_cache_folder = pairing_records_cache_folder" in result
    assert "connection_type=self.connection_type" in result
    assert "pairing_records_cache_folder=self.pairing_records_cache_folder" in result


def test_patch_refuses_mutated_pinned_file_without_writing(tmp_path, monkeypatch):
    source = tmp_path / "userspace_tunnel.py"
    pristine = b"pinned pristine fixture"
    monkeypatch.setattr(patch, "EXPECTED_BEFORE_SHA256", hashlib.sha256(pristine).hexdigest())
    monkeypatch.setattr(patch, "EXPECTED_AFTER_SHA256", "a" * 64)
    source.write_bytes(b"modified upstream source")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="hash does not match"):
        patch.prepare(source, apply=True)
    assert source.read_bytes() == before


def test_pinned_upstream_fixture_transform_is_repeatably_digestable():
    output = patch.transform(FIXTURE).encode("utf-8")
    assert len(hashlib.sha256(output).hexdigest()) == 64
    with pytest.raises(ValueError, match="replacement mismatch"):
        patch.transform(FIXTURE.replace("autopair=autopair", "autopair=False"))
