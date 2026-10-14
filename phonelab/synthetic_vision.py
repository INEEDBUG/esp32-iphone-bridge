"""Small fail-closed pointer detector for already-verified PhoneLab PNGs.

This reports only an image-space candidate. It never reads DOM events, chooses
an action, or establishes that a pixel came from hardware input.
"""
from dataclasses import dataclass
import io
import math

from PIL import Image, ImageChops, ImageCms, ImageFilter, ImageMath

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

from .visual_marker import VisualMarker


_SIZE = (828, 1792)
_MAX_PNG_BYTES = 20 * 1024 * 1024
_MAX_PROFILE_BYTES = 512 * 1024
_NONZERO_TO_ONE = bytes.maketrans(bytes(range(256)), bytes([0] + [1] * 255))


def _connected_components(raw: bytes, width: int, height: int):
    """Yield 8-connected component area, bounds, and edge contact for an L mask."""
    data = bytearray(raw.translate(_NONZERO_TO_ONE))
    cursor = 0

    while True:
        start = data.find(b"\x01", cursor)
        if start < 0:
            break
        cursor = start + 1
        y, x = divmod(start, width)
        left = right = x
        row_start = y * width
        while left > 0 and data[row_start + left - 1]:
            left -= 1
        while right + 1 < width and data[row_start + right + 1]:
            right += 1
        data[row_start + left:row_start + right + 1] = b"\x00" * (right - left + 1)

        area = right - left + 1
        min_x, max_x = left, right
        min_y = max_y = y
        touches_edge = left == 0 or right == width - 1 or y == 0 or y == height - 1
        runs = [(y, left, right)]

        while runs:
            current_y, run_left, run_right = runs.pop()
            for neighbor_y in (current_y - 1, current_y + 1):
                if not 0 <= neighbor_y < height:
                    continue
                neighbor_start = max(0, run_left - 1)
                neighbor_stop = min(width, run_right + 2)
                neighbor_row = neighbor_y * width
                scan = neighbor_row + neighbor_start
                stop = neighbor_row + neighbor_stop
                position = data.find(b"\x01", scan, stop)
                while position >= 0:
                    neighbor_x = position - neighbor_row
                    next_left = next_right = neighbor_x
                    while next_left > 0 and data[neighbor_row + next_left - 1]:
                        next_left -= 1
                    while next_right + 1 < width and data[neighbor_row + next_right + 1]:
                        next_right += 1
                    data[neighbor_row + next_left:neighbor_row + next_right + 1] = (
                        b"\x00" * (next_right - next_left + 1)
                    )
                    run_area = next_right - next_left + 1
                    area += run_area
                    min_x, max_x = min(min_x, next_left), max(max_x, next_right)
                    min_y, max_y = min(min_y, neighbor_y), max(max_y, neighbor_y)
                    if (next_left == 0 or next_right == width - 1
                            or neighbor_y == 0 or neighbor_y == height - 1):
                        touches_edge = True
                    runs.append((neighbor_y, next_left, next_right))
                    scan = neighbor_row + next_right + 2
                    position = data.find(b"\x01", scan, stop)

        yield area, min_x, min_y, max_x, max_y, touches_edge


@dataclass(frozen=True)
class PointerCandidate:
    center_x: float
    center_y: float
    diameter: float
    confidence: float  # Shape score, not a calibrated probability.


def _srgb_image(png):
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > _MAX_PNG_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(png)) as opened:
            if opened.format != "PNG" or opened.size != _SIZE:
                return None
            profile = opened.info.get("icc_profile")
            if profile is None:
                return opened.convert("RGB")
            if not isinstance(profile, bytes) or not profile or len(profile) > _MAX_PROFILE_BYTES:
                return None
            return ImageCms.profileToProfile(
                opened.convert("RGB"), ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                ImageCms.createProfile("sRGB"), outputMode="RGB")
    except Exception:
        return None


def _neutral_masks(image):
    """Exact original RGB tests in Pillow's C operations, without Python pixel loops."""
    red, green, blue = image.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    minimum = ImageChops.darker(ImageChops.darker(red, green), blue)
    neutral = ImageChops.subtract(maximum, minimum).point([int(i <= 24) for i in range(256)])
    # Compare the integer RGB sum to avoid rounding average values at a band edge.
    total = ImageMath.lambda_eval(lambda a: a['red'] + a['green'] + a['blue'],
                                 red=red, green=green, blue=blue)
    dark = ImageMath.lambda_eval(lambda a: (a['total'] >= 210) & (a['total'] < 660),
                                total=total).convert('L')
    light = ImageMath.lambda_eval(lambda a: (a['total'] >= 660) & (a['total'] <= 750),
                                 total=total).convert('L')
    masks = (ImageChops.darker(neutral, dark), ImageChops.darker(neutral, light))
    for mask in masks:
        mask.paste(0, (0, 0, image.width, min(160, image.height)))
        if image.height > 1511:
            mask.paste(0, (0, 1511, image.width, image.height))
    return masks


def _pillow_cleanup_pointer_mask(mask):
    """Reference morphology used when the optional accelerator is unavailable."""
    result = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    for _ in range(5):
        result = result.filter(ImageFilter.MinFilter(3))
    for _ in range(5):
        result = result.filter(ImageFilter.MaxFilter(3))
    return result


def _cleanup_pointer_mask(mask):
    """Apply the exact pointer-mask close/open morphology with optional OpenCV."""
    if cv2 is None or np is None:
        return _pillow_cleanup_pointer_mask(mask)

    pixels = np.asarray(mask, dtype=np.uint8)
    border = cv2.BORDER_REPLICATE
    close_kernel = np.ones((5, 5), dtype=np.uint8)
    opened_kernel = np.ones((3, 3), dtype=np.uint8)
    closed = cv2.morphologyEx(pixels, cv2.MORPH_CLOSE, close_kernel, borderType=border)
    cleaned = cv2.morphologyEx(closed, cv2.MORPH_OPEN, opened_kernel,
                               iterations=5, borderType=border)
    return Image.fromarray(cleaned, mode="L")


def detect_pointer(png: bytes, *, verified_marker: VisualMarker) -> PointerCandidate | None:
    """Return one compact neutral circular candidate, else UNKNOWN (None).

    `verified_marker` must be the result of `decode_visual_marker` for this
    already-associated synthetic page capture. Passing it is an explicit caller
    precondition, not an authentication mechanism. Any ambiguity returns None.
    """
    if (not isinstance(verified_marker, VisualMarker)
            or type(verified_marker.counter) is not int or not 0 <= verified_marker.counter <= 65535
            or not isinstance(verified_marker.bounds, tuple) or len(verified_marker.bounds) != 4
            or any(type(v) is not int for v in verified_marker.bounds)
            or not 30 <= verified_marker.bounds[2] <= 160
            or not 30 <= verified_marker.bounds[3] <= 160
            or verified_marker.bounds[0] < 508 or verified_marker.bounds[1] < 1312
            or verified_marker.bounds[0] + verified_marker.bounds[2] > 828
            or verified_marker.bounds[1] + verified_marker.bounds[3] > 1792
            or type(verified_marker.pixels_per_css_pixel) not in (int, float)
            or not math.isfinite(verified_marker.pixels_per_css_pixel)
            or not 0 < verified_marker.pixels_per_css_pixel <= 10):
        return None
    image = _srgb_image(png)
    if image is None:
        return None

    width, height = image.size
    # Separate bands keep dark/light pointers apart from opposite-polarity
    # backgrounds, using the same exact thresholds as the original pixel loop.
    masks = _neutral_masks(image)

    candidates = []
    for mask in masks:
        # First bridge 1-2px blue seams through a dot, then remove thin gray
        # paragraph strokes touching it. Opening first can erase one side of a
        # split dot and shift its center. Geometry/uniqueness remain unchanged.
        mask = _cleanup_pointer_mask(mask)
        for area, min_x, min_y, max_x, max_y, touches_edge in _connected_components(
                mask.tobytes(), width, height):
            box_w, box_h = max_x - min_x + 1, max_y - min_y + 1
            diameter = (box_w + box_h) / 2
            aspect = min(box_w, box_h) / max(box_w, box_h)
            fill = area / (box_w * box_h)
            bounds = verified_marker.bounds
            overlaps_marker = not (max_x < bounds[0] or min_x >= bounds[0] + bounds[2]
                                   or max_y < bounds[1] or min_y >= bounds[1] + bounds[3])
            if (not touches_edge and not overlaps_marker and 22 <= box_w <= 52 and 22 <= box_h <= 52
                    and aspect >= 0.78 and 0.68 <= fill <= 0.96 and 150 <= area <= 1800):
                # Shape score describes component geometry only, not a probability.
                confidence = min(1.0, max(0.0, 0.55 + 0.25 * aspect + 0.2 * (1 - abs(fill - 0.78))))
                candidates.append(PointerCandidate((min_x + max_x) / 2, (min_y + max_y) / 2,
                                                   diameter, confidence))
                if len(candidates) > 1:
                    return None
    return candidates[0] if len(candidates) == 1 else None
