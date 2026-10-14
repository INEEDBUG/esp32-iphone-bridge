"""Actual pixel focus/caret and local exact-text checks for the synthetic field."""
import math
from PIL import ImageChops

from .image_surface import find_surface
from .page_scope import _synthetic_text_prefix
from .synthetic_vision import _connected_components, _srgb_image


class NativeCaretNotVisible(ValueError):
    """The otherwise valid focused field has no visible full-color caret."""


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
    if not candidates:
        raise NativeCaretNotVisible('native text caret is not visibly present')
    if len(candidates)!=1:
        raise ValueError('native text caret is ambiguous')
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

    def verify_caret(self,png):
        surface=find_surface(png,self.nonce,'text')
        if surface['focused'] is not True:
            raise ValueError('synthetic text field is not visibly focused')
        caret=native_caret(png,surface['bounds'],self.expected_text)
        return {'role':'synthetic-text','focused':True,'caret_at_end':True,
                'text':self.expected_text,'caret':caret,'bounds':surface['bounds']}

    def verify(self,png):
        observed=self.verify_caret(png)
        caret=observed['caret']; bounds=observed['bounds']
        result=self.ocr_reader(png,self.nonce,bounds,self.expected_text,caret)
        if (not isinstance(result,dict) or result.get('text_matches') is not True
                or type(result.get('recognized_length')) is not int
                or result['recognized_length']!=len(self.expected_text)):
            raise ValueError('synthetic field text does not match exactly')
        return {'role':'synthetic-text','focused':True,'caret_at_end':True,'text':self.expected_text}


def select_visible_caret_snapshot(snapshot_reader, guard, *, clock, sleep,
                                   check_cancelled, request_deadline,
                                   timeout=2.5, max_candidates=50):
    """Select a fresh image after caret blink-off, waiting only for zero candidates."""
    if (not callable(snapshot_reader) or not callable(clock) or not callable(sleep)
            or not callable(check_cancelled) or type(request_deadline) not in (int,float)
            or not math.isfinite(request_deadline) or type(timeout) not in (int,float)
            or not 0 < timeout <= 2.5 or type(max_candidates) is not int
            or not 1 <= max_candidates <= 50):
        raise ValueError('invalid bounded caret observation request')
    deadline=min(clock()+timeout,request_deadline)
    for index in range(max_candidates):
        check_cancelled()
        if clock() >= request_deadline:
            raise TimeoutError('original input deadline expired during caret selection')
        if clock() >= deadline:
            raise ValueError('fresh caret candidate unavailable within bounded selection')
        snapshot=snapshot_reader()
        check_cancelled()
        metadata=getattr(snapshot,'metadata',None)
        received=metadata.get('received_monotonic') if isinstance(metadata,dict) else None
        age=clock()-received if type(received) in (int,float) and math.isfinite(received) else None
        if age is None or age < 0:
            raise ValueError('caret candidate image receipt is invalid')
        if age > .25:
            if index+1 >= max_candidates:
                raise ValueError('fresh caret candidate unavailable within bounded selection')
            sleep(min(.05,max(0,deadline-clock())))
            continue
        try:
            guard.verify_caret(snapshot.image_bytes)
            check_cancelled()
            now=clock()
            if now >= request_deadline:
                raise TimeoutError('original input deadline expired during caret selection')
            if now >= deadline:
                raise ValueError('caret analysis exceeded bounded selection deadline')
            age=now-received
            if not 0 <= age <= .25:
                sleep(min(.05,max(0,deadline-clock())))
                continue
            return snapshot
        except NativeCaretNotVisible:
            if clock() >= request_deadline:
                raise TimeoutError('original input deadline expired during caret selection')
            if index+1 >= max_candidates or clock() >= deadline:
                raise
            sleep(min(.05,max(0,deadline-clock())))
    raise NativeCaretNotVisible('no visible native caret within bounded selection')
