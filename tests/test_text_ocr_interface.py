import pytest
from PIL import Image
from test_image_text_guard import text_image
from phonelab.local_english_ocr import read_text_field_ocr


def test_field_ocr_returns_only_exact_match_metadata_and_cleans_private_copy(tmp_path):
    seen=[]
    def recognize(path,expected):
        seen.append((path.is_file(),expected))
        return {'text_matches':True,'recognized_length':7,'raw_text':'must not escape'}
    result=read_text_field_ocr(text_image('test123'),'hello',(120,350,420,470),
        'test123',(312,385,314,425),private_root=tmp_path,reader=recognize)
    assert result=={'text_matches':True,'recognized_length':7}
    assert seen==[(True,'test123')] and not list(tmp_path.iterdir())


def test_private_text_or_invalid_nonce_never_reaches_recognizer(tmp_path):
    calls=[]
    for nonce,text in [('hello','private message'),('wrong','test123')]:
        with pytest.raises(Exception):read_text_field_ocr(text_image('test123'),nonce,
            (120,350,420,470),text,(312,385,314,425),private_root=tmp_path,
            reader=lambda *args:calls.append(args))
    assert not calls


def test_ocr_copy_excludes_only_native_caret_and_contains_only_field_interior(tmp_path):
    seen=[]
    def recognize(path,expected):
        with Image.open(path) as image:
            seen.append((image.size,image.getpixel((312-128,400-356))))
        return {'text_matches':True,'recognized_length':7}
    read_text_field_ocr(text_image('test123'),'hello',(120,350,420,470),'test123',
        (312,385,314,425),private_root=tmp_path,reader=recognize)
    assert seen==[((284,108),(232,255,244))]


def test_empty_field_requires_no_visible_text_ink_and_does_not_use_ocr(tmp_path):
    result=read_text_field_ocr(text_image(''),'hello',(120,350,420,470),'',
        (144,385,146,425),private_root=tmp_path,reader=lambda *args:pytest.fail('blank OCR hallucination'))
    assert result=={'text_matches':True,'recognized_length':0}
