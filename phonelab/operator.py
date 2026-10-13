"""Small, explicit composition seam for operator-approved local control.

No backend is selected at construction or during normal PhoneLab startup. A future
capture provider must independently bind the current USB device and current lock state;
metadata on a JPEG or caller-supplied Frame flags are never sufficient evidence.
"""
from dataclasses import dataclass
import time
import uuid
from pathlib import Path

from phonelab.control import Controller, Frame, LabError
from phonelab import runtime


class OperatorError(RuntimeError):
    pass


TRUSTED_CAPTURE_SOURCE = "USB_CAPTURE_WITH_CURRENT_DEVICE_STATE"
RELATIVE_OPERATIONS = {"move", "scroll"}


@dataclass(frozen=True)
class CaptureProof:
    """Provider-issued current-device evidence tied to one captured frame."""
    target: str
    sequence: int
    target_verified: bool
    lock_state: str
    device_generation: str
    verified_monotonic: float
    source: str


class OperatorSession:
    def __init__(self, target: str, *, private_capture_root: Path,
                 capture_provider=None, data_root: Path = runtime.deploy.DATA,
                 private_root: Path | None = None, image_root: Path = runtime.deploy.ROOT,
                 epoch_reader=None, readiness_fn=runtime.build_readiness,
                 backend_opener=runtime.open_approved):
        if not isinstance(target, str) or not target:
            raise ValueError("a bound target label is required")
        self.target = target
        self.private_capture_root = Path(private_capture_root).resolve()
        self.capture_provider = capture_provider
        self.data_root = Path(data_root)
        self.private_root = Path(private_root) if private_root else self.data_root / "private"
        self.image_root = Path(image_root)
        self.epoch_reader = epoch_reader
        self.readiness_fn = readiness_fn
        self.backend_opener = backend_opener
        self.controller = Controller(target, backend=None)
        self._owned_backend = None
        self._device_generation = None
        self._geometry = None

    def _runtime_args(self):
        return {"data": self.data_root, "private": self.private_root,
                "image_root": self.image_root, "epoch_reader": self.epoch_reader}

    def acquire(self, owner):
        return self.controller.acquire(owner)

    def select_backend(self, name="esp32-ble-serial"):
        if name != "esp32-ble-serial":
            raise OperatorError("unsupported backend selection")
        if self._owned_backend is not None:
            raise OperatorError("an operator-owned backend is already selected")
        try:
            readiness = self.readiness_fn(**self._runtime_args())
        except Exception:
            raise OperatorError("runtime readiness check failed") from None
        if not isinstance(readiness, dict) or readiness.get("ready") is not True:
            raise OperatorError("runtime readiness is not verified")
        try:
            backend = self.backend_opener(**self._runtime_args())
        except Exception:
            raise OperatorError("approved backend open failed") from None
        if backend is None or not callable(getattr(backend, "execute", None)) or not callable(getattr(backend, "close", None)):
            try:
                if backend is not None and callable(getattr(backend, "close", None)):
                    backend.close()
            finally:
                raise OperatorError("opened backend does not satisfy the serial contract")
        self._owned_backend = backend
        self.controller.backend = backend
        return {"selected": True, "backend": getattr(backend, "name", "unknown")}

    def _close_owned(self, reason):
        backend = self._owned_backend
        self._owned_backend = None
        self.controller.backend = None
        self.controller.disconnect(reason)
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

    def stop(self):
        self._close_owned("operator stopped")
        return {"state": "stopped", "release_confirmed": False}

    close = stop

    def watchdog(self):
        """Check only the Controller's cached freshness; never capture, ping, or reopen."""
        result = self.controller.watchdog()
        if result.get("state") == "stopped":
            self._close_owned("watchdog freshness failure")
            return {**result, "backend_closed": True}
        return {**result, "backend_closed": False}

    def get_status(self):
        status = self.controller.get_status()
        status.update({"raw_relative_move": bool(self._owned_backend and self.controller.armed),
                       "raw_vertical_scroll": bool(self._owned_backend and self.controller.armed),
                       "tap_supported": False, "swipe_supported": False,
                       "absolute_pointer_calibration": False})
        return status

    def _invalidate(self, reason):
        self._geometry = None
        self._device_generation = None
        self._close_owned(reason)

    def get_screen(self):
        provider = self.capture_provider
        if provider is None or not callable(getattr(provider, "latest_frame", None)) or not callable(getattr(provider, "verify_current", None)):
            self._invalidate("trusted capture provider unavailable")
            raise OperatorError("trusted current-device capture is unavailable")
        try:
            raw_frame, image_path = provider.latest_frame()
            if not isinstance(raw_frame, Frame):
                raise ValueError("capture provider returned no Frame")
            path = Path(image_path).resolve(strict=True)
            if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg") or not path.is_relative_to(self.private_capture_root):
                raise ValueError("capture image is outside the private capture root")
            if any(part.lower() in ("synthetic", "offline-validation", "airplay-offline-validation") for part in path.parts):
                raise ValueError("synthetic image is not device evidence")
            proof = provider.verify_current(raw_frame, path)
            if not isinstance(proof, CaptureProof):
                raise ValueError("provider did not return typed current-device evidence")
            now = time.monotonic()
            if (proof.source != TRUSTED_CAPTURE_SOURCE or proof.target_verified is not True or
                proof.target != self.target or proof.sequence != raw_frame.sequence or
                proof.lock_state != "UNLOCKED" or not proof.device_generation or
                type(proof.verified_monotonic) not in (int, float) or
                not 0 <= now - proof.verified_monotonic <= self.controller.freshness_seconds):
                raise ValueError("current target/unlocked proof is missing or stale")
            if (raw_frame.width <= 0 or raw_frame.height <= 0 or raw_frame.orientation != "portrait" or
                not 0 <= now - raw_frame.received_monotonic <= self.controller.freshness_seconds):
                raise ValueError("capture geometry or freshness is unsupported")
            geometry = (raw_frame.width, raw_frame.height, raw_frame.orientation)
            if self._device_generation is not None and proof.device_generation != self._device_generation:
                self._invalidate("capture device generation changed")
                raise OperatorError("capture device generation changed; reacquire and reselect backend")
            if self._geometry is not None and geometry != self._geometry:
                self._invalidate("capture geometry changed")
                raise OperatorError("capture geometry changed; backend closed and calibration cleared")
            trusted = Frame(target=proof.target, sequence=raw_frame.sequence,
                received_monotonic=raw_frame.received_monotonic, width=raw_frame.width,
                height=raw_frame.height, orientation=raw_frame.orientation,
                source=TRUSTED_CAPTURE_SOURCE, locked=False)
            self.controller.set_frame(trusted)
            self._device_generation = proof.device_generation
            self._geometry = geometry
            return {"frame": trusted, "private_image_path": path,
                    "capture_source": TRUSTED_CAPTURE_SOURCE,
                    "device_generation_verified": True}
        except OperatorError:
            raise
        except Exception:
            self._invalidate("capture verification failed")
            raise OperatorError("capture failed verification; backend stopped and closed") from None

    def calibrate(self):
        screen = self.get_screen()
        frame = screen["frame"]
        if self._owned_backend is None or self.controller.owner is None:
            raise OperatorError("owner lease and explicit backend selection are required")
        try:
            self.controller.calibrate(frame.width, frame.height, frame.orientation)
        except LabError:
            self._invalidate("operator geometry gate failed")
            raise OperatorError("fresh trusted capture and lease required") from None
        return {"armed": True, "calibration_kind": "geometry_gate_only",
                "relative_move": True, "relative_vertical_scroll": True,
                "tap_supported": False, "swipe_supported": False,
                "absolute_pointer_calibration": False}

    def submit(self, operation: str, args: dict):
        if operation not in RELATIVE_OPERATIONS:
            raise OperatorError("only raw relative move and vertical scroll are exposed; taps/swipes are unsupported")
        if operation == "scroll" and (not isinstance(args, dict) or type(args.get("h")) is not int or args["h"] != 0):
            raise OperatorError("only vertical scrolling is supported")
        if not self._owned_backend or not self.controller.owner or not self.controller.armed:
            raise OperatorError("trusted screen, owner lease, backend, and geometry gate are required")
        request = {"id": uuid.uuid4().hex, "session": self.controller.session,
                   "owner": self.controller.owner, "deadline": time.monotonic() + 4.5,
                   "op": operation, "args": args}
        try:
            result = self.controller.submit(request)
        except Exception:
            self._invalidate("input rejected or transport outcome unknown")
            raise OperatorError("input failed or is unknown; backend closed, never replay") from None
        if not isinstance(result, dict) or result.get("state") not in ("executed", "rejected"):
            self._invalidate("input outcome unknown")
            return {"state": "unknown", "verified": False,
                    "error": "input outcome unknown; backend closed; do not replay"}
        return result
