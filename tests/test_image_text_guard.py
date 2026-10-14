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


def _snapshot(png, received):
    from types import SimpleNamespace
    return SimpleNamespace(image_bytes=png, metadata={'received_monotonic':received})


def test_candidate_selector_waits_only_for_absent_native_caret():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[10.0]
    snapshots=iter([_snapshot(text_image('test123',caret=False),10.0),
                    _snapshot(text_image('test123'),10.05)])
    selected=select_visible_caret_snapshot(lambda: next(snapshots),guard('test123'),
        clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),
        check_cancelled=lambda:None,request_deadline=15.0)
    assert selected.metadata['received_monotonic']==10.05
    assert clock[0] <= 10.2


def test_candidate_selector_fails_finitely_when_native_caret_stays_absent():
    from phonelab.image_text_guard import NativeCaretNotVisible,select_visible_caret_snapshot
    clock=[20.0]; reads=[]
    def read():
        reads.append(clock[0])
        return _snapshot(text_image('test123',caret=False),clock[0])
    with pytest.raises(NativeCaretNotVisible):
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),
            check_cancelled=lambda:None,request_deadline=25.0,timeout=1.2,max_candidates=5)
    assert len(reads)==5
    assert clock[0] <= 21.2


@pytest.mark.parametrize('options',[dict(duplicate=True),dict(focused=False),dict(caret_offset=24)])
def test_candidate_selector_never_waits_for_ambiguity_focus_or_wrong_end(options):
    from phonelab.image_text_guard import NativeCaretNotVisible,select_visible_caret_snapshot
    clock=[30.0]; reads=[]
    def read():
        reads.append(clock[0])
        return _snapshot(text_image('test123',**options),clock[0])
    with pytest.raises(ValueError) as error:
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),
            check_cancelled=lambda:None,request_deadline=35.0)
    assert not isinstance(error.value,NativeCaretNotVisible)
    assert len(reads)==1 and clock[0]==30.0


def test_candidate_selector_propagates_cancellation_and_rejects_stale_candidate():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[40.0]; reads=[]
    def read():
        reads.append(clock[0])
        return _snapshot(text_image('test123',caret=False),clock[0])
    def cancelled():
        if len(reads)>1: raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError,match='cancelled'):
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),check_cancelled=cancelled,
            request_deadline=45.0)
    with pytest.raises(ValueError):
        select_visible_caret_snapshot(lambda:_snapshot(text_image('test123'),39.0),guard('test123'),
            clock=lambda:40.0,sleep=lambda _:None,check_cancelled=lambda:None,request_deadline=45.0)


def test_candidate_selector_cannot_extend_the_original_action_deadline():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[50.0]
    def read(): return _snapshot(text_image('test123',caret=False),clock[0])
    with pytest.raises(TimeoutError,match='original input deadline'):
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),check_cancelled=lambda:None,
            request_deadline=50.08,timeout=1.2)
    assert clock[0] <= 50.08


def test_candidate_selector_skips_stale_capture_then_accepts_fresh_visible_caret():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[60.0]
    snapshots=iter([_snapshot(text_image('test123'),59.7),
                    _snapshot(text_image('test123'),60.05)])
    selected=select_visible_caret_snapshot(lambda:next(snapshots),guard('test123'),
        clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),
        check_cancelled=lambda:None,request_deadline=65.0)
    assert selected.metadata['received_monotonic']==60.05


def test_candidate_selector_fails_finitely_when_every_capture_is_stale():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[70.0];reads=[]
    def read():
        reads.append(clock[0])
        return _snapshot(text_image('test123'),clock[0]-.3)
    with pytest.raises(ValueError,match='fresh caret candidate unavailable'):
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),
            check_cancelled=lambda:None,request_deadline=75.0,max_candidates=4)
    assert len(reads)==4
    assert clock[0] <= 70.2


def test_candidate_selector_rechecks_deadline_after_caret_analysis():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[80.0];image_guard=guard('test123');original=image_guard.verify_caret
    def slow_analysis(png):
        result=original(png);clock[0]+=1.3;return result
    image_guard.verify_caret=slow_analysis
    with pytest.raises(ValueError,match='caret analysis exceeded'):
        select_visible_caret_snapshot(lambda:_snapshot(text_image('test123'),clock[0]),image_guard,
            clock=lambda:clock[0],sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),
            check_cancelled=lambda:None,request_deadline=85.0)


def test_default_candidate_budget_waits_for_delayed_fresh_visible_caret():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[90.0];reads=[];visible_after=1.3
    def read():
        reads.append(clock[0])
        visible=clock[0]-90.0>=visible_after
        return _snapshot(text_image('test123',caret=visible),clock[0])
    selected=select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
        sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),check_cancelled=lambda:None,
        request_deadline=95.0)
    assert selected.metadata['received_monotonic']>=91.3
    assert len(reads)>12 and len(reads)<=50
    assert clock[0]<92.5


def test_default_candidate_budget_still_stops_at_earlier_original_request_deadline():
    from phonelab.image_text_guard import select_visible_caret_snapshot
    clock=[100.0];reads=[]
    def read():
        reads.append(clock[0])
        return _snapshot(text_image('test123',caret=clock[0]-100.0>=1.3),clock[0])
    with pytest.raises(TimeoutError,match='original input deadline'):
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),check_cancelled=lambda:None,
            request_deadline=101.0)
    assert clock[0]<=101.0
    assert len(reads)<=50


def test_default_candidate_budget_remains_finite_for_permanently_missing_caret():
    from phonelab.image_text_guard import NativeCaretNotVisible,select_visible_caret_snapshot
    clock=[110.0];reads=[]
    def read():
        reads.append(clock[0])
        return _snapshot(text_image('test123',caret=False),clock[0])
    with pytest.raises(NativeCaretNotVisible):
        select_visible_caret_snapshot(read,guard('test123'),clock=lambda:clock[0],
            sleep=lambda seconds:clock.__setitem__(0,clock[0]+seconds),check_cancelled=lambda:None,
            request_deadline=120.0)
    assert len(reads)==50
    assert clock[0]<=112.5
