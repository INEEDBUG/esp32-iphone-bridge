"""Small fail-closed pointer detector for already-verified PhoneLab PNGs.

This reports only an image-space candidate. It never reads DOM events, chooses
an action, or establishes that a pixel came from hardware input.
"""
from collections import deque
from dataclasses import dataclass
import io
import math

from PIL import Image, ImageChops, ImageCms, ImageFilter, ImageMath

from .visual_marker import VisualMarker


_SIZE = (828, 1792)
_MAX_PNG_BYTES = 20 * 1024 * 1024
_MAX_PROFILE_BYTES = 512 * 1024


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
        mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
        # Five square 3px erosions/dilations equal one square 11px operation,
        # while Pillow's smaller rank filters finish well within frame expiry.
        for _ in range(5):
            mask = mask.filter(ImageFilter.MinFilter(3))
        for _ in range(5):
            mask = mask.filter(ImageFilter.MaxFilter(3))
        mask = bytearray(mask.tobytes())
        seen = bytearray(width * height)
        for start, active in enumerate(mask):
            if not active or seen[start]:
                continue
            queue = deque((start,))
            seen[start] = 1
            min_x = max_x = start % width
            min_y = max_y = start // width
            area = 0
            touches_edge = False
            while queue:
                index = queue.popleft()
                x, y = index % width, index // width
                area += 1
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                if x == 0 or y == 0 or x == width - 1 or y == height - 1:
                    touches_edge = True
                for dy in (-1, 0, 1):
                    ny = y + dy
                    if not 0 <= ny < height:
                        continue
                    base = ny * width
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx = x + dx
                        if 0 <= nx < width:
                            neighbor = base + nx
                            if mask[neighbor] and not seen[neighbor]:
                                seen[neighbor] = 1
                                queue.append(neighbor)
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
