import io
import pytest
from PIL import Image,ImageDraw
from phonelab.native_hover import verify_unique_blue_hover

BOXES={'home':[250,650,350,680],'device':[450,400,550,430],
       'analysis':[80,400,180,430],'siri':[80,580,180,610]}
ICON=(250,540,350,630)


def fixture(home=True,other=False,icon=True,baseline_blue=False):
    im=Image.new('RGB',(828,1792),(40,40,40));d=ImageDraw.Draw(im)
    baseline=Image.new('RGB',(828,1792),(40,40,40));bd=ImageDraw.Draw(baseline)
    for key,b in BOXES.items():
        color=(0,140,220) if ((key=='home' and home) or (key=='siri' and other)) else (245,245,245)
        if key=='home' and baseline_blue: base_color=(0,140,220)
        else: base_color=(245,245,245)
        # A compact glyph patch inside each caption ROI, not a blue-filled panel.
        rect=(b[0]+20,b[1]+10,b[0]+40,b[1]+20)
        d.rectangle(rect,fill=color);bd.rectangle(rect,fill=base_color)
    if icon:
        d.rectangle((ICON[0]+20,ICON[1]+20,ICON[0]+40,ICON[1]+30),fill=(0,140,220))
    bd.rectangle((ICON[0]+20,ICON[1]+20,ICON[0]+40,ICON[1]+30),fill=(245,245,245))
    def encode(image):
        out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()
    return encode(im),encode(baseline)


def test_native_highlight_can_confirm_target_without_fabricating_pointer():
    current,baseline=fixture()
    result=verify_unique_blue_hover(current,BOXES,'home',ICON,baseline_png=baseline)
    assert result=={'target':'home','evidence':'native_blue_highlight','pointer_position':None}


@pytest.mark.parametrize('flags',[(False,False,True),(True,True,True),(True,False,False)])
def test_missing_or_ambiguous_native_highlight_rejects(flags):
    current,baseline=fixture(*flags)
    with pytest.raises(ValueError):verify_unique_blue_hover(current,BOXES,'home',ICON,baseline_png=baseline)


def test_invalid_or_missing_geometry_rejects():
    current,baseline=fixture()
    with pytest.raises(ValueError):verify_unique_blue_hover(current,{'home':BOXES['home']},'home',ICON,baseline_png=baseline)


def test_static_blue_in_home_baseline_is_not_hover_evidence():
    current,baseline=fixture(baseline_blue=True)
    with pytest.raises(ValueError):verify_unique_blue_hover(current,BOXES,'home',ICON,baseline_png=baseline)


def test_overlapping_icon_and_home_caption_bounds_reject():
    current,baseline=fixture()
    with pytest.raises(ValueError):verify_unique_blue_hover(
        current,BOXES,'home',BOXES['home'],baseline_png=baseline)


def test_baseline_is_required():
    current,_=fixture()
    with pytest.raises(TypeError):verify_unique_blue_hover(current,BOXES,'home',ICON)
