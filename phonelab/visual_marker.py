"""Read-only observation of the synthetic page's changing pixel marker.

This is neither authentication nor lock-state/hardware-input proof. It does not
alter a Frame, arm a controller, inspect text, or send any device operation.
"""
from dataclasses import dataclass
import binascii
import io
import math
import re

from PIL import Image, ImageCms, ImageChops


_MAX_ICC_PROFILE_BYTES = 512 * 1024


class MarkerError(ValueError):
    pass


@dataclass(frozen=True)
class VisualMarker:
    counter: int
    bounds: tuple[int, int, int, int]
    pixels_per_css_pixel: float


def _nonce_hash(nonce):
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", nonce):
        raise MarkerError("invalid synthetic page nonce")
    value = 2166136261
    for byte in nonce.encode("ascii"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def decode_visual_marker(png: bytes, nonce: str) -> VisualMarker:
    expected_hash = _nonce_hash(nonce)
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 20 * 1024 * 1024:
        raise MarkerError("invalid screenshot payload")
    try:
        with Image.open(io.BytesIO(png)) as opened:
            width, height = opened.size
            if width >= height or width > 10000 or height > 10000 or width * height > 12_000_000:
                raise MarkerError("unsupported screenshot geometry")
            profile_bytes = opened.info.get("icc_profile")
            if profile_bytes is None:
                image = opened.convert("RGB")
            else:
                if not isinstance(profile_bytes, bytes) or not profile_bytes or len(profile_bytes) > _MAX_ICC_PROFILE_BYTES:
                    raise MarkerError("invalid or oversized ICC profile")
                source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
                srgb_profile = ImageCms.createProfile("sRGB")
                image = ImageCms.profileToProfile(opened.convert("RGB"), source_profile,
                                                  srgb_profile, outputMode="RGB")
    except MarkerError:
        raise
    except Exception:
        raise MarkerError("invalid screenshot geometry, PNG, or ICC profile") from None

    # The page places its marker in this corner, clear of the safe-area inset.
    # Additional cyan UI in this region makes detection ambiguous and is rejected.
    offset_x, offset_y = max(0, width - 320), max(0, height - 480)
    region = image.crop((offset_x, offset_y, width, height))
    red, green, blue = region.split()
    masks = (red.point(lambda v: 255 if v <= 25 else 0),
             green.point(lambda v: 255 if 175 <= v <= 225 else 0),
             blue.point(lambda v: 255 if 195 <= v <= 245 else 0))
    bounds = ImageChops.multiply(ImageChops.multiply(masks[0], masks[1]), masks[2]).getbbox()
    if bounds is None:
        raise MarkerError("synthetic visual marker unavailable")
    left, top, right, bottom = bounds
    span, strip = right - left, bottom - top
    cell = span / 10
    if not 30 <= span <= 160 or abs(strip - cell) > 1 or top + span > region.height:
        raise MarkerError("synthetic visual marker geometry invalid")

    def pixel(row, column):
        x = left + min(span - 1, int((column + .5) * cell))
        y = top + min(span - 1, int((row + .5) * cell))
        return region.getpixel((x, y))

    def matches(actual, expected):
        return all(abs(a - b) <= 35 for a, b in zip(actual, expected))

    for column in range(10):
        if not matches(pixel(0, column), (0, 200, 220)) or not matches(pixel(9, column), (220, 0, 200)):
            raise MarkerError("synthetic visual marker border invalid")
    payload = bytearray(8)
    for row in range(1, 9):
        if not matches(pixel(row, 0), (250, 200, 0)) or not matches(pixel(row, 9), (30, 80, 240)):
            raise MarkerError("synthetic visual marker orientation invalid")
        for column in range(1, 9):
            color = pixel(row, column)
            if all(v < 75 for v in color):
                payload[row - 1] |= 1 << (8 - column)
            elif not all(v > 200 for v in color):
                raise MarkerError("synthetic visual marker cell occluded")
    if (int.from_bytes(payload[:4], "big") != expected_hash or
            binascii.crc_hqx(payload[:6], 0xFFFF) != int.from_bytes(payload[6:], "big")):
        raise MarkerError("synthetic visual marker run or checksum mismatch")
    return VisualMarker(int.from_bytes(payload[4:6], "big"),
                        (offset_x + left, offset_y + top, span, span), span / 40)


class VisualMarkerTracker:
    """Require actual content progression; repeated image fetches never renew it."""
    def __init__(self, nonce: str):
        _nonce_hash(nonce)
        self.nonce = nonce
        self._reset()

    def _reset(self):
        self._sequence = 0
        self._received = None
        self._counter = None
        self._changed = None
        self._progress = False

    def observe(self, png: bytes, *, sequence: int, received: float, now: float):
        try:
            if (type(sequence) is not int or sequence < 1 or sequence < self._sequence
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in (received, now))
                    or not 0 <= now - received <= 2
                    or (self._received is not None and received < self._received)
                    or (sequence == self._sequence and received != self._received)):
                raise MarkerError("invalid capture sequence or receipt age")
            marker = decode_visual_marker(png, self.nonce)
            if self._counter is not None and marker.counter < self._counter:
                raise MarkerError("visual counter reset; use a new page session")
            if self._counter is not None and marker.counter > self._counter:
                if sequence == self._sequence:
                    raise MarkerError("content changed without a new capture sequence")
                self._progress = True
                self._changed = received
            elif self._counter is None:
                self._changed = received
            self._sequence, self._received, self._counter = sequence, received, marker.counter
            state = ("stale_content" if now - self._changed > 1.5 else
                     "progress_observed" if self._progress else "pending_progress")
            return {"state": state, "counter": marker.counter, "pixel_bounds": marker.bounds,
                    "pixels_per_css_pixel": marker.pixels_per_css_pixel,
                    "lock_state": "UNKNOWN", "input_verified": False,
                    "source_time_available": False}
        except Exception:
            self._reset()
            raise
