"""Optional local dependency integration on generated images, never a device test."""
import io
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
import pytest
from phonelab.deploy import DATA
from phonelab.image_text_guard import ImageTextFieldGuard
from phonelab.local_english_ocr import prepare_text_ocr,close_text_ocr
from test_image_text_guard import text_image


@pytest.fixture(scope='module')
def local_ocr():
    project=Path(__file__).resolve().parents[1]
    if not (project/'scripts/local-ocr/node_modules/tesseract.js').is_dir() or not (DATA/'tooling/local-ocr/tessdata/eng.traineddata').is_file():
        pytest.skip('optional local English OCR not installed; no automatic download')
    if not Path(r'C:\Windows\Fonts\cour.ttf').is_file():pytest.skip('fixture font unavailable')
    prepare_text_ocr()
    yield
    close_text_ocr()


def render(value):
    image=Image.open(io.BytesIO(text_image(value))).convert('RGB')
    if value:ImageDraw.Draw(image).text((144,382),value,font=ImageFont.truetype(r'C:\Windows\Fonts\cour.ttf',40),fill=(23,32,51))
    stream=io.BytesIO();image.save(stream,format='PNG');return stream.getvalue()


@pytest.mark.parametrize('value',['','test123','TEST123','Test 123','test123!@#'])
def test_generated_native_field_focus_caret_and_offline_ocr_match_exactly(local_ocr,value):
    assert ImageTextFieldGuard('hello',value).verify(render(value))['text']==value


@pytest.mark.parametrize('value',['test128','TEST123','test124','test1234'])
def test_changed_generated_text_cannot_pass_expected_test123(local_ocr,value):
    with pytest.raises(ValueError):ImageTextFieldGuard('hello','test123').verify(render(value))
