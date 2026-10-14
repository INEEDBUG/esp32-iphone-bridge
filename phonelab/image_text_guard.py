"""Actual pixel focus/caret and local exact-text checks for the synthetic field."""
from PIL import ImageChops

from .image_surface import find_surface
from .page_scope import _synthetic_text_prefix
from .synthetic_vision import _connected_components, _srgb_image


def native_caret(png, bounds, expected_text):
    image = _srgb_image(png)
    left, top, right, bottom = bounds
    region = image.crop((left+10, top+8, right-10, bottom-8))
    channels = region.split()
    limits = ((200,245),(10,55),(45,105))
    masks = [channel.point([255 if lo <= value <= hi else 0 for value in range(256)])
             for channel,(lo,hi) in zip(channels,limits)]
    mask=ImageChops.multiply(ImageChops.multiply(masks[0],masks[1]),masks[2])
    candidates=[]
    for area,x0,y0,x1,y1,_ in _connected_components(mask.tobytes(),*region.size):
        width,height=x1-x0+1,y1-y0+1
        if 1 <= width <= 5 and 25 <= height <= 60 and area >= height*.6:
            candidates.append((x0+left+10,y0+top+8,x1+left+11,y1+top+9))
    if len(candidates)!=1:
        raise ValueError('native text caret missing or ambiguous')
    caret=candidates[0]
    # Lab CSS is Courier New 20 CSS px, 12px advance, 12px inner padding;
    # capture is exactly 2x. Reject horizontally clipped/selected/non-end text.
    expected_x=left+24+len(expected_text)*24
    if (not left+14 < expected_x < right-12
            or abs((caret[0]+caret[2]-1)/2-expected_x)>8):
        raise ValueError('native caret is not at the expected visible text end')
    return caret


class ImageTextFieldGuard:
    def __init__(self, nonce, expected_text, *, ocr_reader=None):
        if not _synthetic_text_prefix(expected_text):
            raise ValueError('only approved synthetic phrase prefixes are supported')
        if ocr_reader is None:
            from .local_english_ocr import read_text_field_ocr
            ocr_reader=read_text_field_ocr
        self.nonce,self.expected_text,self.ocr_reader=nonce,expected_text,ocr_reader

    def verify(self,png):
        surface=find_surface(png,self.nonce,'text')
        if surface['focused'] is not True:
            raise ValueError('synthetic text field is not visibly focused')
        caret=native_caret(png,surface['bounds'],self.expected_text)
        result=self.ocr_reader(png,self.nonce,surface['bounds'],self.expected_text,caret)
        if (not isinstance(result,dict) or result.get('text_matches') is not True
                or type(result.get('recognized_length')) is not int
                or result['recognized_length']!=len(self.expected_text)):
            raise ValueError('synthetic field text does not match exactly')
        return {'role':'synthetic-text','focused':True,'caret_at_end':True,'text':self.expected_text}
