import io
from PIL import Image, ImageDraw
import pytest

from test_image_surface import _page, BOX


def text_image(text='', *, focused=True, caret=True, caret_offset=0, duplicate=False):
    image = Image.open(io.BytesIO(_page(focused=focused))).convert('RGB')
    draw = ImageDraw.Draw(image)
    x = BOX[0] + 24 + len(text) * 24 + caret_offset
    if caret:
        draw.rectangle((x, BOX[1]+35, x+1, BOX[1]+74), fill=(225,29,72))
    if duplicate:
        draw.rectangle((x+12, BOX[1]+35, x+13, BOX[1]+74), fill=(225,29,72))
    out=io.BytesIO(); image.save(out,format='PNG'); return out.getvalue()


def guard(text='', **kwargs):
    from phonelab.image_text_guard import ImageTextFieldGuard
    return ImageTextFieldGuard('hello', text, ocr_reader=lambda *args: {
        'text_matches': True, 'recognized_length': len(text)}, **kwargs)


def test_native_caret_focus_and_exact_approved_text_are_all_required():
    for text in ('', 'test123', 'TEST123', 'Test 123'):
        result=guard(text).verify(text_image(text))
        assert result=={'role':'synthetic-text','focused':True,'caret_at_end':True,'text':text}


@pytest.mark.parametrize('options', [dict(focused=False),dict(caret=False),
    dict(caret_offset=-24),dict(caret_offset=24),dict(duplicate=True)])
def test_absent_or_wrong_native_caret_or_focus_is_rejected(options):
    with pytest.raises(ValueError): guard('test123').verify(text_image('test123',**options))


def test_ocr_mismatch_and_private_or_unsupported_expected_text_are_rejected():
    from phonelab.image_text_guard import ImageTextFieldGuard
    with pytest.raises(ValueError): ImageTextFieldGuard('hello','private message')
    wrong=ImageTextFieldGuard('hello','test123',ocr_reader=lambda *args:{'text_matches':False,'recognized_length':7})
    with pytest.raises(ValueError):wrong.verify(text_image('test123'))


def test_wrong_nonce_and_non_end_caret_never_call_ocr():
    from phonelab.image_text_guard import ImageTextFieldGuard
    calls=[]
    wrong=ImageTextFieldGuard('other','',ocr_reader=lambda *args:calls.append(args))
    with pytest.raises(ValueError):wrong.verify(text_image())
    wrong=ImageTextFieldGuard('hello','test123',ocr_reader=lambda *args:calls.append(args))
    with pytest.raises(ValueError):wrong.verify(text_image('test123',caret_offset=-24))
    assert not calls
