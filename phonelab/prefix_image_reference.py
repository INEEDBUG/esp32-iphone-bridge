"""Exact, image-only references for approved synthetic text prefixes.

A verified image is historical glyph evidence. It does not establish current
input state; every candidate must independently pass current nonce, focus, and
native-caret checks before exact field-pixel comparison.
"""
from dataclasses import dataclass, field
from .image_surface import find_surface
from .image_text_guard import ImageTextFieldGuard, native_caret
from .page_scope import _SYNTHETIC_TEXT_PHRASES, _synthetic_text_prefix
from .synthetic_vision import _srgb_image


_FILL = (232, 255, 244)
_RED = (225, 29, 72)
_INTERIOR = (8, 6, 8, 6)


def _field_bytes(image, bounds):
    left, top, right, bottom = bounds
    pad_left, pad_top, pad_right, pad_bottom = _INTERIOR
    return image.crop((left + pad_left, top + pad_top,
                       right - pad_right, bottom - pad_bottom)).tobytes()


def _caret_mask(bounds, caret):
    left, top, right, bottom = caret
    x0, y0 = max(bounds[0] + 8, left - 2), max(bounds[1] + 6, top - 2)
    x1, y1 = min(bounds[2] - 8, right + 2), min(bounds[3] - 6, bottom + 2)
    if not (x0 < x1 and y0 < y1 and x1 - x0 <= 9 and y1 - y0 <= 64):
        raise ValueError('native caret mask dimensions are invalid')
    return x0, y0, x1, y1


def _caret_blend(pixel):
    if pixel == _FILL:
        return False
    alpha = (255 - pixel[1]) / (255 - _RED[1])
    if not 0 <= alpha <= 1.02:
        return False
    predicted = tuple(round(base + (target - base) * alpha)
                      for base, target in zip(_FILL, _RED))
    return all(abs(actual - expected) <= 3
               for actual, expected in zip(pixel, predicted))


def _normalize_source_caret(image, bounds, caret):
    box = _caret_mask(bounds, caret)
    pixels = image.load()
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            pixel = pixels[x, y]
            if pixel != _FILL and _caret_blend(pixel):
                pixels[x, y] = _FILL
    return box


def _pack_field(image, bounds):
    return _field_bytes(image, bounds)


@dataclass(frozen=True, init=False)
class VerifiedPrefixImage:
    """Immutable exact glyph reference made from a fully OCR-verified PNG."""

    _nonce: str = field(repr=False)
    full_text: str
    _bounds: tuple
    _size: tuple
    _rgb: bytes = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError('use from_png to create a verified prefix reference')

    @classmethod
    def from_png(cls, png, *, nonce, full_text):
        if (not isinstance(nonce, str) or not nonce
                or not isinstance(full_text, str)
                or full_text not in _SYNTHETIC_TEXT_PHRASES
                or not _synthetic_text_prefix(full_text)):
            raise ValueError('only an approved full synthetic phrase can be referenced')
        guard = ImageTextFieldGuard(nonce, full_text)
        guard.verify(png)
        image = _srgb_image(png)
        surface = find_surface(png, nonce, 'text')
        bounds = surface['bounds']
        caret = native_caret(png, bounds, full_text)
        _normalize_source_caret(image, bounds, caret)
        instance = object.__new__(cls)
        object.__setattr__(instance, '_nonce', nonce)
        object.__setattr__(instance, 'full_text', full_text)
        object.__setattr__(instance, '_bounds', bounds)
        object.__setattr__(instance, '_size', image.size)
        object.__setattr__(instance, '_rgb', _pack_field(image, bounds))
        return instance

    def verify(self, png, expected_prefix, *, nonce=None):
        if (not isinstance(expected_prefix, str)
                or not self.full_text.startswith(expected_prefix)
                or not _synthetic_text_prefix(expected_prefix)):
            raise ValueError('requested text is not a reference prefix')
        if nonce is not None and nonce != self._nonce:
            raise ValueError('synthetic image nonce does not match reference')
        image = _srgb_image(png)
        if image is None or image.size != self._size:
            raise ValueError('synthetic image dimensions do not match reference')
        surface = find_surface(png, self._nonce, 'text')
        if surface['focused'] is not True or surface['bounds'] != self._bounds:
            raise ValueError('synthetic text field is not the referenced focused field')
        bounds = surface['bounds']
        caret = native_caret(png, bounds, expected_prefix)
        caret_box = _caret_mask(bounds, caret)

        width = bounds[2] - bounds[0] - 16
        height = bounds[3] - bounds[1] - 12
        row_bytes = width * 3
        expected = bytearray(self._rgb)
        keep = min(width, 16 + len(expected_prefix) * 24)
        for y in range(height):
            row = y * row_bytes
            expected[row + keep * 3:row + row_bytes] = bytes([*_FILL]) * (width - keep)
        # Preserve exact expected glyph pixels; normalize caret blends only over fill.
        x0, y0, x1, y1 = caret_box
        for y in range(y0, y1):
            for x in range(x0, x1):
                local_x, local_y = x - bounds[0] - 8, y - bounds[1] - 6
                offset = (local_y * width + local_x) * 3
                expected_pixel = tuple(expected[offset:offset + 3])
                actual_pixel = image.getpixel((x, y))
                if actual_pixel == expected_pixel:
                    continue
                if expected_pixel == _FILL and _caret_blend(actual_pixel):
                    image.putpixel((x, y), _FILL)
                    continue
                raise ValueError('caret pixels overlap or differ from expected glyphs')
        if _field_bytes(image, bounds) != bytes(expected):
            raise ValueError('synthetic text prefix pixels do not match exactly')
        return {'text_matches': True, 'recognized_length': len(expected_prefix)}

    def ocr_reader(self, png, nonce, bounds, expected, caret):
        """Compatibility adapter for ImageTextFieldGuard injection; uses pixels only."""
        if nonce != self._nonce:
            raise ValueError('synthetic image nonce does not match reference')
        if bounds != self._bounds or not isinstance(expected, str):
            raise ValueError('synthetic field geometry or expected text is invalid')
        image = _srgb_image(png)
        if image is None:
            raise ValueError('synthetic image is invalid')
        surface = find_surface(png, self._nonce, 'text')
        if surface['bounds'] != bounds or surface['focused'] is not True:
            raise ValueError('synthetic text field is not the referenced focused field')
        actual_caret = native_caret(png, bounds, expected)
        if caret != actual_caret:
            raise ValueError('native caret argument does not match current image')
        return self.verify(png, expected)
