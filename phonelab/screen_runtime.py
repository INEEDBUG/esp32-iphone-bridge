"""Explicit, one-shot owner for the private USB screenshot provider."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import math
import threading
import time
from pathlib import Path

from .control import Frame
from .iphone import read_target_binding

_PNG = b"\x89PNG\r\n\x1a\n"
_MAX_PNG_BYTES = 20 * 1024 * 1024
_PROVIDER_SOURCE = "Apple DVT screenshot over existing USB pairing and in-process RSD"
_DEPENDENCY_LOG_ROOTS = ("pymobiledevice3", "pmd_pytcp", "pmd_net_addr")
_FRAME_VALIDATION_CODES = frozenset({
    "FRAME_RESULT_INVALID", "FRAME_PATH_INVALID", "FRAME_TARGET_INVALID",
    "FRAME_LOCK_STATE_INVALID", "FRAME_SEQUENCE_INVALID", "FRAME_DIMENSIONS_INVALID",
    "FRAME_ORIENTATION_INVALID", "FRAME_SOURCE_INVALID", "FRAME_AGE_INVALID",
    "FRAME_AGE_STALE", "FRAME_AGE_FUTURE", "FRAME_PNG_INVALID",
    "FRAME_GENERATION_INVALID",
})


class FrameValidationError(ValueError):
    """Safe, fixed-code frame validation failure; never embeds private values."""

    def __init__(self, code):
        self.code = code if code in _FRAME_VALIDATION_CODES else "FRAME_RESULT_INVALID"
        super().__init__("capture frame failed validation")


class _DependencyLogGuard:
    """Temporarily silence dependency loggers without changing unrelated logging state."""

    def __init__(self):
        self._saved = []

    def install(self):
        names = {name for name, logger in list(logging.Logger.manager.loggerDict.items())
                 if isinstance(logger, logging.Logger)
                 and any(name == root or name.startswith(root + ".") for root in _DEPENDENCY_LOG_ROOTS)}
        names.update(_DEPENDENCY_LOG_ROOTS)
        try:
            for name in sorted(names):
                logger = logging.getLogger(name)
                previous_handlers = list(logger.handlers)
                previous_level = logger.level
                previous_propagate = logger.propagate
                owned = logging.NullHandler()
                for handler in previous_handlers:
                    logger.removeHandler(handler)
                logger.addHandler(owned)
                logger.setLevel(logging.CRITICAL + 1)
                logger.propagate = False
                self._saved.append((logger, previous_handlers, previous_level, previous_propagate, owned))
        except BaseException:
            self.restore()
            raise

    def restore(self):
        for logger, handlers, level, propagate, owned in reversed(self._saved):
            logger.removeHandler(owned)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
        self._saved.clear()


class ScreenCaptureRuntime:
    """Runs exactly one USB capture provider on a private async loop; never reconnects."""

    def __init__(self, data_root: Path, *, provider_factory=None, interval: float = 0.5,
                 freshness_seconds: float = 2.0, clock=None):
        self.data_root = Path(data_root)
        self._provider_factory = provider_factory
        self.interval = interval
        self.freshness_seconds = freshness_seconds
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None
        self._provider = None
        self._state = "UNKNOWN"
        self._target = None
        self._frame = None
        self._png = None
        self._generation = None
        self._cleanup_failed = False
        self._async_failed = False
        self._cancel_requested = False
        self._teardown_started = False
        self._async_failure_codes = []
        self._async_failure_phases = []
        self._failure_code = "NONE"
        self._failure_phase = "NONE"
        self._failure_record_path = None
        self._phase = "NOT_STARTED"
        self._frame_drop_code = "NONE"
        self._frame_drop_count = 0
        self._consecutive_stale_frames = 0

    def _binding(self):
        return read_target_binding(self.data_root)

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("USB capture runtime is one-shot")
            serial, target = self._binding()
            self._target = target
            self._state = "starting"
            self._thread = threading.Thread(target=self._thread_main, args=(serial, target),
                                            name="phonelab-usb-screen-capture", daemon=True)
            self._thread.start()
        if not self._ready.wait(25):
            self.close()
            raise TimeoutError("USB screen capture did not produce a verified first frame")
        with self._lock:
            no_frame = self._frame is None
        if no_frame:
            self.close()
            raise RuntimeError("USB screen capture failed before its first frame")

    def _thread_main(self, serial: str, target: str):
        # This provider uses sockets only. Keep Windows Proactor teardown resets
        # out of this private loop without changing the global asyncio policy.
        loop = asyncio.SelectorEventLoop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        previous_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._async_exception_handler)
        task = loop.create_task(self._run_provider(serial, target))
        self._task = task
        guard = _DependencyLogGuard()
        try:
            guard.install()
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._record_failure(exc, "CAPTURE_LOOP")
            with self._lock:
                self._clear_locked("failed" if self._cleanup_failed or not self._stop.is_set() else "stopped")
                self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for item in pending:
                    item.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.set_exception_handler(previous_exception_handler)
                loop.close()
                asyncio.set_event_loop(None)
            finally:
                guard.restore()
                with self._lock:
                    if self._state not in {"failed", "stopped"}:
                        self._clear_locked("stopped")
                    self._ready.set()

    def _async_exception_handler(self, loop, _context):
        # asyncio context can contain device endpoints, callback reprs, and traceback locals.
        # Never format or log it; retain only a boolean failure state.
        with self._lock:
            exception = _context.get('exception')
            known = {asyncio.CancelledError: 'CANCELLED', asyncio.InvalidStateError: 'INVALID_STATE',
                     TimeoutError: 'TIMEOUT', ConnectionResetError: 'CONNECTION_RESET',
                     ConnectionAbortedError: 'CONNECTION_ABORTED', OSError: 'OS_ERROR',
                     RuntimeError: 'RUNTIME_ERROR'}
            code = known.get(type(exception), 'OTHER_EXCEPTION' if exception is not None else 'NO_EXCEPTION')
            if code not in self._async_failure_codes and len(self._async_failure_codes) < 5:
                self._async_failure_codes.append(code)
            phase = ('LOOP_DRAIN' if self._task is not None and self._task.done() else
                     'PROVIDER_TEARDOWN' if self._teardown_started else
                     'STOPPING_CAPTURE' if self._stop.is_set() else 'CAPTURE')
            if phase not in self._async_failure_phases and len(self._async_failure_phases) < 4:
                self._async_failure_phases.append(phase)
            self._async_failed = True
            self._clear_locked("failed")
            self._ready.set()
            self._stop.set()
        self._record_failure(exception, phase)
        self._cancel_capture_once()

    def _record_failure(self, exception, phase):
        phases = {"CAPTURE_LOOP", "PROVIDER_CREATE", "OPEN", "CAPTURE", "FRAME_VALIDATE",
                  "WAIT", "PROVIDER_CLOSE", "LOOP_DRAIN", "PROVIDER_TEARDOWN", "STOPPING_CAPTURE"}
        known = {asyncio.CancelledError: "CANCELLED", asyncio.InvalidStateError: "INVALID_STATE",
                 TimeoutError: "TIMEOUT", ConnectionResetError: "CONNECTION_RESET",
                 ConnectionAbortedError: "CONNECTION_ABORTED", OSError: "OS_ERROR",
                 RuntimeError: "RUNTIME_ERROR", ValueError: "VALUE_ERROR",
                 TypeError: "TYPE_ERROR", ImportError: "IMPORT_ERROR",
                 AttributeError: "ATTRIBUTE_ERROR"}
        if isinstance(exception, FrameValidationError):
            code = exception.code
        else:
            code = None
        try:
            from .usb_capture import CaptureError
            if code is None:
                code = "CAPTURE_ERROR" if isinstance(exception, CaptureError) else None
        except Exception:
            code = None
        if code is None:
            code = "NO_EXCEPTION" if exception is None else known.get(type(exception), "OTHER_EXCEPTION")
        safe_phase = phase if phase in phases else "UNKNOWN"
        with self._lock:
            if self._failure_code == "NONE":
                self._failure_code = code
                self._failure_phase = safe_phase
        # A private, atomic record keeps only fixed enums and booleans. In
        # particular, never serialize exception text, reprs, or tracebacks.
        private = self.data_root / "private"
        temporary = None
        try:
            root = self.data_root.resolve()
            if not private.is_dir() or private.resolve().parent != root:
                return
            with self._lock:
                if self._failure_record_path is None:
                    self._failure_record_path = private / f"capture-failure-{time.monotonic_ns()}.json"
                destination = self._failure_record_path
                record = {"failure_code": self._failure_code, "failure_phase": self._failure_phase,
                          "async_failed": bool(self._async_failed),
                          "cleanup_failed": bool(self._cleanup_failed),
                          "frame_drop_code": self._frame_drop_code,
                          "frame_drop_count": self._frame_drop_count,
                          "consecutive_stale_frames": self._consecutive_stale_frames}
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
            temporary.replace(destination)
        except Exception:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _cancel_capture_once(self):
        # Runs on the capture loop. A second cancellation can interrupt an exit-stack
        # callback that has already been removed and cannot be retried safely.
        with self._lock:
            task = self._task
            if (task is not None and not task.done() and not self._cancel_requested
                    and not self._teardown_started):
                self._cancel_requested = True
                task.cancel()

    async def _run_provider(self, serial: str, target: str):
        provider = None
        output = self.data_root / "private" / "usb-frames"
        phase = "PROVIDER_CREATE"
        self._phase = phase
        try:
            if self._provider_factory is None:
                from .usb_capture import USBScreenCapture
                factory = USBScreenCapture
            else:
                factory = self._provider_factory
            provider = factory(serial=serial, target=target, data_root=self.data_root,
                               output_directory=output)
            self._provider = provider
            phase = self._phase = "OPEN"
            await asyncio.wait_for(provider.open(), timeout=20)
            with self._lock:
                self._state = "running"
            while not self._stop.is_set():
                phase = self._phase = "CAPTURE"
                result = await asyncio.wait_for(provider.capture(), timeout=15)
                phase = self._phase = "FRAME_VALIDATE"
                if self._stop.is_set():
                    break
                try:
                    accepted = self._accept(result, target, output)
                except FrameValidationError as exc:
                    if exc.code != "FRAME_AGE_STALE":
                        raise
                    with self._lock:
                        self._frame_drop_code = exc.code
                        self._frame_drop_count += 1
                        self._consecutive_stale_frames += 1
                        self._png = None
                        self._state = "stale"
                        consecutive = self._consecutive_stale_frames
                    self._record_failure(exc, phase)
                    if consecutive >= 3:
                        raise
                    continue
                if not accepted:
                    break
                self._ready.set()
                try:
                    phase = self._phase = "WAIT"
                    await asyncio.wait_for(asyncio.to_thread(self._stop.wait, self.interval),
                                           timeout=self.interval + 0.25)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(exc, phase)
            with self._lock:
                self._clear_locked("failed" if self._async_failed or not self._stop.is_set() else "stopped")
            self._ready.set()
        finally:
            with self._lock:
                self._teardown_started = True
            self._phase = "PROVIDER_CLOSE"
            try:
                if provider is not None:
                    await asyncio.wait_for(provider.close(), timeout=8)
            except BaseException as exc:
                with self._lock:
                    self._cleanup_failed = True
                    self._clear_locked("failed")
                self._record_failure(exc, "PROVIDER_CLOSE")
                raise

    def _accept(self, result, target: str, output: Path):
        required = {"frame", "private_path", "image_bytes", "generation"}
        if not isinstance(result, dict) or not required <= set(result):
            raise FrameValidationError("FRAME_RESULT_INVALID")
        frame = result["frame"]
        try:
            path = Path(result["private_path"]).resolve(strict=True)
            output_resolved = output.resolve(strict=True)
            path_ok = path.is_relative_to(output_resolved) and path.is_file()
        except (OSError, RuntimeError, TypeError, ValueError):
            path_ok = False
        if not path_ok:
            raise FrameValidationError("FRAME_PATH_INVALID")
        png = result["image_bytes"]
        generation = result["generation"]
        if not isinstance(frame, Frame):
            raise FrameValidationError("FRAME_RESULT_INVALID")
        if frame.target != target:
            raise FrameValidationError("FRAME_TARGET_INVALID")
        if frame.locked is not None:
            raise FrameValidationError("FRAME_LOCK_STATE_INVALID")
        if type(frame.sequence) is not int or frame.sequence < 1:
            raise FrameValidationError("FRAME_SEQUENCE_INVALID")
        if (type(frame.width) is not int or type(frame.height) is not int
                or not (1 <= frame.width <= 10000 and 1 <= frame.height <= 10000)):
            raise FrameValidationError("FRAME_DIMENSIONS_INVALID")
        if frame.orientation not in {"portrait", "landscape"}:
            raise FrameValidationError("FRAME_ORIENTATION_INVALID")
        if frame.source not in {"USB_SCREENSHOT", _PROVIDER_SOURCE} or frame.source_time is not None:
            raise FrameValidationError("FRAME_SOURCE_INVALID")
        if type(frame.received_monotonic) not in (int, float) or not math.isfinite(frame.received_monotonic):
            raise FrameValidationError("FRAME_AGE_INVALID")
        if not isinstance(png, bytes) or not png.startswith(_PNG) or len(png) > _MAX_PNG_BYTES:
            raise FrameValidationError("FRAME_PNG_INVALID")
        if not isinstance(generation, str) or not generation:
            raise FrameValidationError("FRAME_GENERATION_INVALID")
        with self._lock:
            if self._stop.is_set():
                return False
            if self._generation is not None and generation != self._generation:
                raise FrameValidationError("FRAME_GENERATION_INVALID")
            if self._frame is not None and frame.sequence <= self._frame.sequence:
                raise FrameValidationError("FRAME_SEQUENCE_INVALID")
            age = self._clock() - frame.received_monotonic
            if age < 0:
                raise FrameValidationError("FRAME_AGE_FUTURE")
            if age > self.freshness_seconds:
                raise FrameValidationError("FRAME_AGE_STALE")
            self._generation = generation
            self._frame = replace(frame, source="USB_SCREENSHOT", locked=None)
            self._png = png
            self._state = "running"
            self._consecutive_stale_frames = 0
            return True

    def _clear_locked(self, state: str):
        self._frame = None
        self._png = None
        self._generation = None
        self._state = state

    def _is_fresh_locked(self):
        return bool(self._frame and 0 <= self._clock() - self._frame.received_monotonic <= self.freshness_seconds)

    def screen(self):
        snapshot = self.snapshot()
        return snapshot['image_bytes'] if snapshot else None

    def snapshot(self):
        """Return one immutable frame and its matching metadata under the same lock."""
        with self._lock:
            if self._state != "running" or not self._is_fresh_locked():
                return None
            metadata = self.status()
            if not metadata['fresh']:
                return None
            return {'image_bytes': self._png, 'metadata': metadata}

    def status(self):
        with self._lock:
            fresh = self._state == "running" and self._is_fresh_locked()
            frame = self._frame if fresh else None
            state = "fresh" if fresh else (self._state if self._state in {"UNKNOWN", "failed", "stopped", "starting"} else "stale")
            result = {"state": state, "target": self._target, "sequence": None,
                      "width": None, "height": None, "orientation": None,
                      "source": None, "source_time": None, "received_monotonic": None,
                      "locked": "UNKNOWN", "fresh": fresh}
            if frame:
                result.update(sequence=frame.sequence, width=frame.width, height=frame.height,
                              orientation=frame.orientation, source=frame.source,
                              received_monotonic=frame.received_monotonic)
            return result

    def close(self):
        # All callers wait for the same owner instead of cancelling its teardown.
        with self._close_lock:
            self._close_once()

    def shutdown_status(self):
        """Coarse teardown diagnostics only; never asyncio reprs, messages or endpoints."""
        with self._lock:
            cleanup = getattr(self._provider, 'cleanup_status', 'UNKNOWN')
            if cleanup not in {'NOT_OPEN', 'OPENING', 'CLOSED', 'FAILED'}:
                cleanup = 'UNKNOWN'
            return {'state': self._state, 'async_failed': self._async_failed,
                    'cleanup_failed': self._cleanup_failed, 'provider_cleanup': cleanup,
                    'failure_code': self._failure_code, 'failure_phase': self._failure_phase,
                    'frame_drop_code': self._frame_drop_code,
                    'frame_drop_count': self._frame_drop_count,
                    'consecutive_stale_frames': self._consecutive_stale_frames,
                    'owner_thread_alive': bool(self._thread and self._thread.is_alive()),
                    'async_failure_codes': list(self._async_failure_codes),
                    'async_failure_phases': list(self._async_failure_phases),
                    'cancel_requested': self._cancel_requested,
                    'teardown_started': self._teardown_started}

    def _close_once(self):
        with self._lock:
            thread = self._thread
            if thread is None:
                self._clear_locked("stopped")
                self._ready.set()
                return
            self._stop.set()
            loop, task = self._loop, self._task
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(self._cancel_capture_once)
        thread.join(timeout=10)
        if thread.is_alive():
            with self._lock:
                self._clear_locked("failed")
            raise TimeoutError("USB capture owner did not close within the shutdown bound")
        with self._lock:
            if self._cleanup_failed or self._async_failed:
                self._clear_locked("failed")
                raise RuntimeError("USB capture cleanup failed" if self._cleanup_failed
                                   else "USB capture async loop failed")
            self._clear_locked("stopped")
