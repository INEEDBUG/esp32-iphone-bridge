"""Pinned, reversible source adaptation for this project's local pymobiledevice3 venv."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

EXPECTED_VERSION = "11.29.0"
EXPECTED_BEFORE_SHA256 = "6d2908f23e4d1cc7829ea57509bdbb6f4a15aa5055066a240a913e0fd8eb4eca"
# Filled with the digest of the deterministic transformation below.
EXPECTED_AFTER_SHA256 = "5ddb6f0aa6fad8995923f5b6556a7c3223d62ac88809884c3000cf4a91fd7e29"
RELATIVE_SOURCE = Path("pymobiledevice3/remote/userspace_tunnel.py")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def transform(source: str) -> str:
    """Apply exactly five pinned edits; reject source drift and partial matches."""
    edits = (
        (
            "from functools import partial\nfrom typing import Any, Callable, Optional, Protocol, TypeVar, cast\n",
            "from functools import partial\nfrom pathlib import Path\nfrom typing import Any, Callable, Optional, Protocol, TypeVar, cast\n",
        ),
        (
            "async def _create_no_root_tunnel_provider(serial: Optional[str], autopair: bool, remotepairing_fallback: bool = True):",
            "async def _create_no_root_tunnel_provider(\n"
            "    serial: Optional[str], autopair: bool, remotepairing_fallback: bool = True,\n"
            "    connection_type: Optional[str] = None, pairing_records_cache_folder: Optional[Path] = None,\n"
            "):",
        ),
        (
            "    lockdown = await create_using_usbmux(serial=serial, autopair=autopair)\n",
            "    lockdown = await create_using_usbmux(\n"
            "        serial=serial, autopair=autopair, connection_type=connection_type,\n"
            "        pairing_records_cache_folder=pairing_records_cache_folder,\n"
            "    )\n",
        ),
        (
            "    def __init__(\n"
            "        self, serial: Optional[str] = None, autopair: bool = True, remotepairing_fallback: bool = True\n"
            "    ) -> None:\n"
            "        self.serial = serial\n"
            "        self.autopair = autopair\n"
            "        self.remotepairing_fallback = remotepairing_fallback\n",
            "    def __init__(\n"
            "        self, serial: Optional[str] = None, autopair: bool = True, remotepairing_fallback: bool = True,\n"
            "        connection_type: Optional[str] = None, pairing_records_cache_folder: Optional[Path] = None,\n"
            "    ) -> None:\n"
            "        self.serial = serial\n"
            "        self.autopair = autopair\n"
            "        self.remotepairing_fallback = remotepairing_fallback\n"
            "        self.connection_type = connection_type\n"
            "        self.pairing_records_cache_folder = pairing_records_cache_folder\n",
        ),
        (
            "                self.serial, self.autopair, self.remotepairing_fallback\n",
            "                self.serial, self.autopair, self.remotepairing_fallback,\n"
            "                connection_type=self.connection_type,\n"
            "                pairing_records_cache_folder=self.pairing_records_cache_folder,\n",
        ),
    )
    result = source
    for old, new in edits:
        count = result.count(old)
        if count != 1:
            raise ValueError(f"Pinned source replacement mismatch (expected one, found {count}).")
        result = result.replace(old, new, 1)
    return result


def prepare(source_path: Path, *, apply: bool) -> dict[str, str | bool]:
    path = source_path.resolve(strict=True)
    original = path.read_bytes()
    if sha256(original) == EXPECTED_AFTER_SHA256 and EXPECTED_AFTER_SHA256:
        return {"state": "already_patched", "sha256": EXPECTED_AFTER_SHA256, "changed": False}
    if sha256(original) != EXPECTED_BEFORE_SHA256:
        raise ValueError("Installed source hash does not match the pinned pristine pymobiledevice3 11.29.0 file.")
    text = original.decode("utf-8")
    patched = transform(text).encode("utf-8")
    after = sha256(patched)
    if EXPECTED_AFTER_SHA256 and after != EXPECTED_AFTER_SHA256:
        raise ValueError("Deterministic patch output digest differs from the pinned result.")
    if apply:
        path.write_bytes(patched)
    return {"state": "applied" if apply else "preview", "before": EXPECTED_BEFORE_SHA256,
            "after": after, "changed": apply}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the verified patch")
    args = parser.parse_args()
    try:
        result = prepare(args.source, apply=args.apply)
    except Exception as exc:
        print(f"USB tunnel patch refused: {type(exc).__name__}")
        return 2
    print(f"{result['state']}: changed={result['changed']} sha256={result.get('after', result.get('sha256'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
