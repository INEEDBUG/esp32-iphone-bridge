"""Explicit, read-only iPhone screen capture over an existing USB pairing.

There is no import-time work, auto-connect, retry, DDI mount, TSS request, or input
operation. Callers must explicitly open a provider and keep returned frame bytes private.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from phonelab.control import Frame
from phonelab.paths import resolve_data_root

TARGET_MODEL = "iPhone12,1"
TARGET_OS = "17.5.1"
TARGET_BUILD = "21F90"
USB_TUNNEL_PATCH_SHA256 = "5ddb6f0aa6fad8995923f5b6556a7c3223d62ac88809884c3000cf4a91fd7e29"
USB_TUNNEL_PRISTINE_SHA256 = "6d2908f23e4d1cc7829ea57509bdbb6f4a15aa5055066a240a913e0fd8eb4eca"
MAX_FRAMES = 21600
MAX_RING_FRAMES = 60
MAX_SESSION_SECONDS = 10800
MAX_FRAME_BYTES = 30 * 1024 * 1024
TASK_DATA_ROOT = resolve_data_root()


class CaptureError(RuntimeError):
    """Sanitized capture failure without phone identifiers or response data."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_tunnel_source(
    source_path: Path,
    pristine_path: Path,
    expected_source_sha256: str = USB_TUNNEL_PATCH_SHA256,
    expected_pristine_sha256: str = USB_TUNNEL_PRISTINE_SHA256,
) -> bool:
    """Fail closed unless the project venv has the reviewed USB-only patch and backup."""
    try:
        matches = (
            _sha256(source_path) == expected_source_sha256
            and _sha256(pristine_path) == expected_pristine_sha256
        )
    except OSError:
        matches = False
    if not matches:
        raise CaptureError("The isolated USB tunnel source or pristine backup changed")
    return True


def _validate_frame(payload: bytes) -> tuple[int, int]:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_FRAME_BYTES:
        raise CaptureError("Screen frame was empty or oversized")
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG":
                raise ValueError
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
    except Exception:
        raise CaptureError("Screen frame was not a valid PNG") from None
    if width < 1 or height < 1 or width > 10000 or height > 10000:
        raise CaptureError("Screen frame dimensions were outside the supported range")
    return width, height


class USBScreenCapture:
    """Async USB/RSD/DVT capture provider with a private, bounded PNG ring.

    ``serial`` is required so usbmux cannot select the first attached device. The provider
    never reveals it in metadata. It uses only an existing pairing record, and does not start
    another connection if opening or capture fails.
    """

    def __init__(
        self,
        *,
        serial: str,
        target: str,
        data_root: Path = TASK_DATA_ROOT,
        output_directory: Path,
        ring_size: int = 8,
        max_frames: int = MAX_FRAMES,
        session_timeout: float = MAX_SESSION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        screenshot_timeout: float = 10,
        tunnel_factory: Callable[..., Any] | None = None,
        dvt_factory: Callable[..., Any] | None = None,
        screenshot_factory: Callable[..., Any] | None = None,
        dependency_verifier: Callable[[], Any] | None = None,
    ):
        if not isinstance(serial, str) or not serial or len(serial) > 64:
            raise CaptureError("An exact USB target serial is required")
        expected_target = "iphone-" + hashlib.sha256(serial.encode("utf-8")).hexdigest()[:12]
        if not isinstance(target, str) or not re.fullmatch(r"iphone-[0-9a-f]{12}", target) or target != expected_target:
            raise CaptureError("Target label does not bind to the exact USB identifier")
        if not 1 <= ring_size <= min(max_frames, MAX_RING_FRAMES) or max_frames > MAX_FRAMES:
            raise CaptureError("Capture limits are outside the supported bounds")
        if not 1 <= session_timeout <= MAX_SESSION_SECONDS:
            raise CaptureError("Capture session timeout is outside the supported bounds")
        if not 0.01 <= screenshot_timeout <= 10:
            raise CaptureError("Screenshot timeout is outside the supported bounds")
        self.serial = serial
        self.target = target
        self.data_root = Path(data_root).resolve()
        self.output_directory = Path(output_directory).resolve()
        private_root = (self.data_root / "private").resolve()
        if self.data_root != TASK_DATA_ROOT.resolve() or not self.output_directory.is_relative_to(private_root):
            raise CaptureError("Capture files must stay under this task's private data directory")
        self.pairing_cache = private_root / "pairing-cache"
        self.ring_size = ring_size
        self.max_frames = max_frames
        self.session_timeout = session_timeout
        self.clock = clock
        self.screenshot_timeout = screenshot_timeout
        self.session_id = secrets.token_hex(8)
        self._tunnel_factory = tunnel_factory
        self._dvt_factory = dvt_factory
        self._screenshot_factory = screenshot_factory
        self._dependency_verifier = dependency_verifier
        self._stack: AsyncExitStack | None = None
        self._close_task: asyncio.Task | None = None
        self._screenshot: Any = None
        self._opened = False
        self._closed = False
        self._started = 0.0
        self._sequence = 0
        self._capture_lock = asyncio.Lock()
        self.cleanup_status = "NOT_OPEN"

    async def __aenter__(self) -> "USBScreenCapture":
        await self.open()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    def _verify_dependency(self) -> None:
        if self._dependency_verifier is not None:
            if self._dependency_verifier() is not True:
                raise CaptureError("The isolated USB tunnel source verification failed")
            return
        project_root = Path(__file__).resolve().parents[1]
        remote_dir = project_root / ".venv/Lib/site-packages/pymobiledevice3/remote"
        validate_tunnel_source(
            remote_dir / "userspace_tunnel.py",
            remote_dir / "userspace_tunnel.py.phonelab-pristine",
        )

    async def open(self) -> None:
        if self._opened or self._closed:
            raise CaptureError("Capture provider cannot be reopened")
        self._verify_dependency()
        try:
            self.cleanup_status = "OPENING"
            if self._tunnel_factory is None:
                from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

                tunnel_factory = UserspaceRsdTunnel
            else:
                tunnel_factory = self._tunnel_factory
            if self._dvt_factory is None:
                from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

                dvt_factory = DvtProvider
            else:
                dvt_factory = self._dvt_factory
            if self._screenshot_factory is None:
                from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot

                screenshot_factory = Screenshot
            else:
                screenshot_factory = self._screenshot_factory

            self._stack = AsyncExitStack()
            await self._stack.__aenter__()
            tunnel = tunnel_factory(
                serial=self.serial,
                autopair=False,
                remotepairing_fallback=False,
                connection_type="USB",
                pairing_records_cache_folder=self.pairing_cache,
            )
            rsd = await self._stack.enter_async_context(tunnel)
            identity = (
                getattr(rsd, "udid", None),
                getattr(rsd, "product_type", None),
                getattr(rsd, "product_version", None),
                getattr(rsd, "product_build_version", None),
            )
            if identity != (self.serial, TARGET_MODEL, TARGET_OS, TARGET_BUILD):
                raise CaptureError("USB target did not match the approved frozen device")
            dvt = await self._stack.enter_async_context(dvt_factory(rsd))
            self._screenshot = await self._stack.enter_async_context(screenshot_factory(dvt))
            self.output_directory.mkdir(parents=True, exist_ok=True)
            self._started = self.clock()
            self._opened = True
        except BaseException as exc:
            try:
                await self.close()
            except CaptureError:
                raise
            if isinstance(exc, CaptureError) or not isinstance(exc, Exception):
                raise
            raise CaptureError("USB screenshot service could not be opened") from None

    async def capture(self) -> dict[str, Any]:
        if not self._opened or self._closed or self._screenshot is None:
            raise CaptureError("Capture provider is not open")
        async with self._capture_lock:
            if self.clock() - self._started >= self.session_timeout:
                await self.close()
                raise CaptureError("Capture session time limit reached")
            if self._sequence >= self.max_frames:
                await self.close()
                raise CaptureError("Capture session frame limit reached")
            try:
                async with asyncio.timeout(self.screenshot_timeout):
                    payload = await self._screenshot.get_screenshot()
                width, height = _validate_frame(payload)
            except BaseException as exc:
                try:
                    await self.close()
                except CaptureError:
                    raise
                if isinstance(exc, CaptureError) or not isinstance(exc, Exception):
                    raise
                raise CaptureError("USB screenshot request failed") from None

            self._sequence += 1
            captured_monotonic = self.clock()
            received_utc = datetime.now(timezone.utc).isoformat()
            ring_dir = self.output_directory / "ring"
            slot = (self._sequence - 1) % self.ring_size
            path = ring_dir / f"frame-{slot:02d}.png"
            temp_path = ring_dir / f"frame-{slot:02d}.{self.session_id}.tmp"
            try:
                ring_dir.mkdir(parents=True, exist_ok=True)
                temp_path.write_bytes(payload)
                temp_path.replace(path)
            except OSError:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                await self.close()
                raise CaptureError("Private screen frame could not be stored") from None
            orientation = "portrait" if height > width else "landscape" if width > height else "square"
            frame = Frame(
                target=self.target,
                sequence=self._sequence,
                received_monotonic=captured_monotonic,
                width=width,
                height=height,
                orientation=orientation,
                source_time=None,
                source="Apple DVT screenshot over existing USB pairing and in-process RSD",
                locked=None,
            )
            return {
                "frame": frame,
                "private_path": path,
                "image_bytes": payload,
                "generation": self.session_id,
                "received_utc": received_utc,
                "lock_state": "UNKNOWN",
                "input_performed": False,
            }

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._opened = False
            stack, self._stack = self._stack, None
            self._close_task = asyncio.create_task(self._finish_close(stack))
        cancelled = False
        while True:
            try:
                await asyncio.shield(self._close_task)
                break
            except asyncio.CancelledError:
                if self._close_task.cancelled():
                    raise CaptureError("USB screenshot service teardown was interrupted") from None
                # An exit-stack callback is popped before it is awaited. Keep the
                # single cleanup task alive, then preserve caller cancellation.
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _finish_close(self, stack: AsyncExitStack | None) -> None:
        try:
            if stack is not None:
                async with asyncio.timeout(7):
                    await stack.aclose()
        except BaseException as exc:
            self.cleanup_status = "FAILED"
            self._screenshot = None
            if not isinstance(exc, Exception):
                raise
            raise CaptureError("USB screenshot service teardown failed") from None
        self._screenshot = None
        self.cleanup_status = "CLOSED"
