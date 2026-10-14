"""Fail-closed image geometry for fixed synthetic PhoneLab surfaces.

This identifies a unique colored surface on a nonce-marked screenshot. It does
not read text, infer caret state, choose coordinates, or establish input effect.
"""
from PIL import ImageChops

from .synthetic_vision import _connected_components, _srgb_image
from .visual_marker import decode_visual_marker


_SCREEN = (828, 1792)
_SAFE_BOUNDS = (20, 180, 808, 1450)
_SURFACES = {
    "text": ((232, 255, 244), (100, 116, 139), (0, 133, 119)),
    "drag-source": ((255, 242, 212), (180, 100, 20), None),
    "drop-target": ((242, 248, 237), (70, 130, 60), None),
    "native-scroll": ((236, 242, 250), (65, 85, 110), None),
}


def _color_mask(image, color):
    channels = image.split()
    matched = [
        channel.point([255 if abs(value - expected) <= 3 else 0
                       for value in range(256)])
        for channel, expected in zip(channels, color)
    ]
    return ImageChops.multiply(ImageChops.multiply(matched[0], matched[1]), matched[2])


def _components(image, fill):
    width, height = image.size
    candidates = []
    for area, left, top, right_inclusive, bottom_inclusive, _ in _connected_components(
            _color_mask(image, fill).tobytes(), width, height):
        right, bottom = right_inclusive + 1, bottom_inclusive + 1
        box_width, box_height = right - left, bottom - top
        if (80 <= box_width <= 780 and 60 <= box_height <= 400
                and area >= 2000 and area / (box_width * box_height) >= .55):
            candidates.append((area, (left, top, right, bottom)))
    return candidates


def _side_support(image, bounds, color):
    left, top, right, bottom = bounds
    height, width = bottom - top, right - left
    x_start, x_stop = left + width // 4, right - width // 4
    y_start, y_stop = top + height // 4, bottom - height // 4
    pixels = image.load()

    def supported(points):
        total = len(points)
        return total > 0 and sum(
            all(abs(actual - expected) <= 3 for actual, expected in zip(pixels[x, y], color))
            for x, y in points
        ) / total >= .80

    left_ok = any(supported([(left - offset, y) for y in range(y_start, y_stop)])
                  for offset in range(1, 7))
    right_ok = any(supported([(right + offset - 1, y) for y in range(y_start, y_stop)])
                   for offset in range(1, 7))
    top_ok = any(supported([(x, top - offset) for x in range(x_start, x_stop)])
                 for offset in range(1, 7))
    bottom_ok = any(supported([(x, bottom + offset - 1) for x in range(x_start, x_stop)])
                    for offset in range(1, 7))
    return left_ok, top_ok, right_ok, bottom_ok


def _safe(bounds):
    left, top, right, bottom = bounds
    safe_left, safe_top, safe_right, safe_bottom = _SAFE_BOUNDS
    return (safe_left <= left < right <= safe_right
            and safe_top <= top < bottom <= safe_bottom)


def find_surface(png: bytes, nonce: str, role: str) -> dict:
    """Return unique synthetic surface bounds and, for text, uniform focus.

    Bounds use image pixels with an exclusive right/bottom edge. Only a
    unique, connected fill and a complete matching four-sided border qualify.
    """
    if not isinstance(role, str) or role not in _SURFACES:
        raise ValueError("synthetic surface role invalid")
    try:
        decode_visual_marker(png, nonce)
    except Exception:
        raise ValueError("synthetic surface marker invalid") from None
    image = _srgb_image(png)
    if image is None or image.size != _SCREEN:
        raise ValueError("synthetic surface image invalid")

    fill, inactive_border, active_border = _SURFACES[role]
    candidates = _components(image, fill)
    if len(candidates) != 1:
        raise ValueError("synthetic surface missing or ambiguous")
    _, bounds = candidates[0]
    if not _safe(bounds):
        raise ValueError("synthetic surface outside safe image bounds")

    if role == "text":
        inactive_sides = _side_support(image, bounds, inactive_border)
        active_sides = _side_support(image, bounds, active_border)
        if all(inactive_sides):
            focused = False
        elif all(active_sides):
            focused = True
        else:
            raise ValueError("synthetic text surface border missing or mixed")
    else:
        if not all(_side_support(image, bounds, inactive_border)):
            raise ValueError("synthetic surface border missing or ambiguous")
        focused = None

    return {"role": role, "bounds": bounds, "focused": focused}
