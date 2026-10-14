import io
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import pytest

from phonelab.prefix_image_reference import VerifiedPrefixImage, _caret_mask
from phonelab.image_surface import find_surface
from phonelab.image_text_guard import native_caret
from phonelab.synthetic_vision import _srgb_image
from test_image_surface import BOX, _page

NONCE = 'hello'
FONT = Path(r'C:\Windows\Fonts\cour.ttf')
FILL = (232, 255, 244)
INK = (23, 32, 51)
RED = (225, 29, 72)


def dark(pixel):
    return pixel[0] < 80 and pixel[1] < 100 and pixel[2] < 120


def png(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def rendered(text, *, focused=True, caret=True, caret_offset=0, duplicate=False, text_offset=0):
    image = Image.open(io.BytesIO(_page(focused=focused))).convert('RGB')
    draw = ImageDraw.Draw(image)
    if text:
        draw.text((BOX[0] + 24 + text_offset, BOX[1] + 32), text,
                  font=ImageFont.truetype(str(FONT), 40), fill=INK)
    x = BOX[0] + 24 + len(text) * 24 + caret_offset
    if caret:
        draw.rectangle((x, BOX[1] + 35, x + 1, BOX[1] + 74), fill=RED)
    if duplicate:
        draw.rectangle((x + 12, BOX[1] + 35, x + 13, BOX[1] + 74), fill=RED)
    return png(image)


def reference(monkeypatch, text='test123', *, result=None):
    from phonelab import local_english_ocr
    if result is None:
        result = {'text_matches': True, 'recognized_length': len(text)}
    monkeypatch.setattr(local_english_ocr, 'read_text_field_ocr', lambda *_: result)
    return VerifiedPrefixImage.from_png(rendered(text), nonce=NONCE, full_text=text)


def test_verified_reference_accepts_exact_rendered_prefix_and_blank(monkeypatch):
    ref = reference(monkeypatch)
    assert ref.verify(rendered('test1'), 'test1') == {
        'text_matches': True, 'recognized_length': 5}
    assert ref.verify(rendered(''), '') == {
        'text_matches': True, 'recognized_length': 0}
    assert ref.verify(rendered('test123'), 'test123') == {
        'text_matches': True, 'recognized_length': 7}


def test_reference_creation_requires_exact_original_guard_ocr(monkeypatch):
    with pytest.raises(ValueError):
        reference(monkeypatch, result={'text_matches': False, 'recognized_length': 7})


@pytest.mark.parametrize('text', ['test2', 'Test1', 'test12', 'test1x'])
def test_current_frame_must_match_reference_prefix_pixel_for_pixel(text, monkeypatch):
    with pytest.raises(ValueError):
        reference(monkeypatch).verify(rendered(text), 'test1')


def test_current_frame_rejects_wrong_nonce_focus_or_caret_position(monkeypatch):
    ref = reference(monkeypatch)
    with pytest.raises(ValueError): ref.verify(rendered('test1'), 'test1', nonce='wrong')
    with pytest.raises(ValueError): ref.verify(rendered('test1', focused=False), 'test1')
    with pytest.raises(ValueError): ref.verify(rendered('test1', caret_offset=24), 'test1')


def test_current_frame_rejects_duplicate_caret_pointer_and_single_rgb_change(monkeypatch):
    ref = reference(monkeypatch)
    with pytest.raises(ValueError): ref.verify(rendered('test1', duplicate=True), 'test1')
    image = Image.open(io.BytesIO(rendered('test1'))).convert('RGB')
    ImageDraw.Draw(image).ellipse((BOX[0] + 200, BOX[1] + 60, BOX[0] + 207, BOX[1] + 67), fill=(30, 180, 220))
    with pytest.raises(ValueError): ref.verify(png(image), 'test1')
    image = Image.open(io.BytesIO(rendered('test1'))).convert('RGB')
    x, y = BOX[0] + 60, BOX[1] + 50
    r, g, b = image.getpixel((x, y))
    image.putpixel((x, y), (r, g, b - 1))
    with pytest.raises(ValueError): ref.verify(png(image), 'test1')


def test_ocr_compatibility_reader_never_authorizes_a_different_nonce(monkeypatch):
    ref = reference(monkeypatch)
    with pytest.raises(ValueError):
        ref.ocr_reader(rendered('test1'), 'wrong', BOX, 'test1', None)


def test_current_caret_antialias_halo_is_removed_only_inside_validated_box(monkeypatch):
    ref = reference(monkeypatch)
    image = Image.open(io.BytesIO(rendered('test1'))).convert('RGB')
    x = BOX[0] + 24 + len('test1') * 24
    blend = tuple(round((a + b) / 2) for a, b in zip(FILL, RED))
    for y in range(BOX[1] + 40, BOX[1] + 45):
        image.putpixel((x - 1, y), blend)
    assert ref.verify(png(image), 'test1')['text_matches'] is True


def test_dark_glyph_pixel_inside_caret_mask_is_rejected(monkeypatch):
    ref = reference(monkeypatch)
    image = Image.open(io.BytesIO(rendered('test1'))).convert('RGB')
    x = BOX[0] + 24 + len('test1') * 24
    image.putpixel((x - 1, BOX[1] + 45), INK)
    with pytest.raises(ValueError):
        ref.verify(png(image), 'test1')


def test_factory_accepts_only_complete_approved_phrases_and_hides_nonce(monkeypatch):
    from phonelab import local_english_ocr
    monkeypatch.setattr(local_english_ocr, 'read_text_field_ocr',
        lambda *_: {'text_matches': True, 'recognized_length': 5})
    with pytest.raises(ValueError):
        VerifiedPrefixImage.from_png(rendered('test1'), nonce=NONCE, full_text='test1')
    assert NONCE not in repr(reference(monkeypatch))


def test_direct_constructor_cannot_fabricate_verified_reference():
    with pytest.raises(TypeError, match='from_png'):
        VerifiedPrefixImage(NONCE, 'test123', BOX, (828, 1792), b'fabricated')


def test_matching_neighboring_glyph_stroke_inside_expanded_caret_box_is_preserved(monkeypatch):
    from phonelab import local_english_ocr
    monkeypatch.setattr(local_english_ocr, 'read_text_field_ocr',
        lambda *_: {'text_matches': True, 'recognized_length': 7})
    shifted_full = rendered('test123', text_offset=4)
    ref = VerifiedPrefixImage.from_png(shifted_full, nonce=NONCE, full_text='test123')
    shifted_prefix = rendered('test1', text_offset=4)
    bounds = find_surface(shifted_prefix, NONCE, 'text')['bounds']
    caret = native_caret(shifted_prefix, bounds, 'test1')
    box = _caret_mask(bounds, caret)
    image = _srgb_image(shifted_prefix)
    assert any(dark(image.getpixel((x, y)))
               for y in range(box[1], box[3]) for x in range(box[0], box[2]))
    assert ref.verify(shifted_prefix, 'test1')['text_matches'] is True



@pytest.mark.parametrize('mutation', ['missing', 'changed', 'extra', 'occluded'])
def test_caret_box_stroke_must_remain_exact_and_visible(monkeypatch, mutation):
    from phonelab import local_english_ocr
    monkeypatch.setattr(local_english_ocr, 'read_text_field_ocr',
        lambda *_: {'text_matches': True, 'recognized_length': 7})
    ref = VerifiedPrefixImage.from_png(rendered('test123', text_offset=4),
        nonce=NONCE, full_text='test123')
    image = Image.open(io.BytesIO(rendered('test1', text_offset=4))).convert('RGB')
    bounds = find_surface(png(image), NONCE, 'text')['bounds']
    caret = native_caret(png(image), bounds, 'test1')
    x0, y0, x1, y1 = _caret_mask(bounds, caret)
    glyph_pixel = next((x, y) for y in range(y0, y1) for x in range(x0, x1)
                       if dark(image.getpixel((x, y))))
    blank_pixel = next((x, y) for y in range(y0, y1) for x in range(x0, x1)
                       if image.getpixel((x, y)) == FILL)
    x, y = glyph_pixel if mutation != 'extra' else blank_pixel
    if mutation == 'missing':
        image.putpixel((x, y), FILL)
    elif mutation == 'changed':
        old = image.getpixel((x, y))
        image.putpixel((x, y), (old[0] + 1, old[1], old[2]))
    elif mutation == 'extra':
        image.putpixel((x, y), INK)
    else:
        image.putpixel((x, y), RED)
    with pytest.raises(ValueError):
        ref.verify(png(image), 'test1')
