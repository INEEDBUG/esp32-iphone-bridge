"""Locate a circular overlay against an independently enrolled stable scene.

This image primitive does not prove scene identity or authorize input. Callers
must independently verify the surrounding scene and track a bounded local ROI.
"""
from dataclasses import dataclass

from PIL import ImageChops

from .synthetic_vision import _cleanup_pointer_mask, _connected_components, _srgb_image


@dataclass(frozen=True)
class PointerOverlay:
    center_x: float
    center_y: float
    diameter: float


def find_pointer_overlay(png, baseline_png, *, search_bounds):
    """Return one circle-sized changed component, or None when ambiguous.

    The baseline must depict the same scene with the pointer absent from the
    searched region. Changed text, animations and scene transitions can invalidate
    this assumption; this detector is never a substitute for a scene guard.
    """
    current = _srgb_image(png)
    baseline = _srgb_image(baseline_png)
    if current is None or baseline is None or current.size != baseline.size:
        raise ValueError('matching valid images required')
    if (not isinstance(search_bounds, tuple) or len(search_bounds) != 4
            or any(type(value) is not int for value in search_bounds)):
        raise ValueError('four integer search bounds required')
    left, top, right, bottom = search_bounds
    if not (0 <= left < right <= current.width and 0 <= top < bottom <= current.height):
        raise ValueError('search bounds must be inside the image')
    difference = ImageChops.difference(current.crop(search_bounds), baseline.crop(search_bounds))
    red, green, blue = difference.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = maximum.point([int(value >= 14) for value in range(256)])
    cleaned = _cleanup_pointer_mask(mask)
    width, height = cleaned.size
    candidates = []
    for area, min_x, min_y, max_x, max_y, touches_edge in _connected_components(
            cleaned.tobytes(), width, height):
        box_width, box_height = max_x - min_x + 1, max_y - min_y + 1
        aspect = min(box_width, box_height) / max(box_width, box_height)
        fill = area / (box_width * box_height)
        if (not touches_edge and 22 <= box_width <= 52 and 22 <= box_height <= 52
                and aspect >= .78 and .68 <= fill <= .96 and 150 <= area <= 1800):
            candidates.append(PointerOverlay(
                left + (min_x + max_x) / 2,
                top + (min_y + max_y) / 2,
                (box_width + box_height) / 2,
            ))
        elif area >= 20:
            # A sizeable unrelated change invalidates the enrolled local scene.
            return None
    return candidates[0] if len(candidates) == 1 else None
