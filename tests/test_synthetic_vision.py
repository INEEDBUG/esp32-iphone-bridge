import io

from PIL import Image, ImageCms, ImageDraw
import pytest

from phonelab.synthetic_vision import detect_pointer
from phonelab.visual_marker import VisualMarker


def screenshot(*, cursor=None, background=(245, 245, 245), extra=()):
    image = Image.new("RGB", (828, 1792), background)
    draw = ImageDraw.Draw(image)
    # Synthetic page furniture: blue text-like strokes and a large floating control.
    draw.rounded_rectangle((20, 700, 806, 950), radius=26, fill=(255, 255, 255))
    draw.ellipse((20, 20, 140, 140), fill=(185, 187, 190))
    for x in range(70, 760, 31):
        draw.rectangle((x, 780, x + 13, 785), fill=(30, 70, 150))
    for center, radius, color in (*extra, *((cursor,) if cursor else ())):
        x, y = center
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def screenshot_cursor_touching_button_border():
    image = Image.new("RGB", (828, 1792), (239, 244, 251))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((170, 1250, 260, 1340), radius=18,
                           fill=(238, 244, 255), outline=(38, 99, 220), width=5)
    # The circular overlay touches the blue border and its antialiased pale edge.
    draw.ellipse((222, 1276, 260, 1314), fill=(145, 148, 154))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def screenshot_cursor_cut_by_two_blue_seams():
    image = Image.new("RGB", (828, 1792), (239, 244, 251))
    draw = ImageDraw.Draw(image)
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((269, 1227, 307, 1265), fill=(128, 130, 136, 160))
    image = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    draw = ImageDraw.Draw(image)
    # Two 2px blue seams mimic adjacent rounded probe borders through the dot.
    draw.rectangle((280, 1227, 281, 1265), fill=(38, 99, 220))
    draw.rectangle((300, 1227, 301, 1265), fill=(38, 99, 220))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


MARKER = VisualMarker(counter=1, bounds=(728, 1692, 80, 80), pixels_per_css_pixel=2.0)


@pytest.mark.parametrize("background,cursor_color", [
    ((245, 245, 245), (110, 110, 110)),
    ((120, 120, 120), (235, 235, 235)),
])
def test_detects_one_dark_or_light_compact_pointer_without_reporting_dom_target(background, cursor_color):
    result = detect_pointer(screenshot(cursor=((414, 1100), 18, cursor_color), background=background),
                            verified_marker=MARKER)
    assert result is not None
    assert (result.center_x, result.center_y) == (414, 1100)
    assert 32 <= result.diameter <= 40
    assert 0 < result.confidence <= 1
    assert not hasattr(result, "target")


def test_large_floating_button_and_text_are_not_mistaken_for_pointer():
    png = screenshot(background=(245, 245, 245), extra=(((80, 250), 60, (185, 187, 190)),))
    assert detect_pointer(png, verified_marker=MARKER) is None


def test_hollow_neutral_letter_o_is_not_a_solid_pointer_candidate():
    image = Image.new("RGB", (828, 1792), (245, 245, 245))
    ImageDraw.Draw(image).ellipse((396, 1082, 432, 1118), outline=(110, 110, 110), width=4)
    out = io.BytesIO()
    image.save(out, format="PNG")
    assert detect_pointer(out.getvalue(), verified_marker=MARKER) is None


def test_missing_multiple_or_edge_clipped_pointer_is_unknown():
    assert detect_pointer(screenshot(), verified_marker=MARKER) is None
    multiple = screenshot(extra=(((300, 1100), 18, (110, 110, 110)),
                                 ((500, 1100), 18, (110, 110, 110))))
    assert detect_pointer(multiple, verified_marker=MARKER) is None
    clipped = screenshot(cursor=((8, 1100), 18, (110, 110, 110)))
    assert detect_pointer(clipped, verified_marker=MARKER) is None


def test_wrong_geometry_or_unverified_marker_is_unknown():
    wrong = Image.new("RGB", (827, 1792), (245, 245, 245))
    out = io.BytesIO()
    wrong.save(out, format="PNG")
    assert detect_pointer(out.getvalue(), verified_marker=MARKER) is None
    assert detect_pointer(screenshot(cursor=((414, 1100), 18, (110, 110, 110))),
                          verified_marker=None) is None


def test_embedded_color_profile_is_normalized_before_pointer_checks():
    png = screenshot(cursor=((414, 1100), 18, (110, 110, 110)))
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.save(out, format="PNG", icc_profile=ImageCms.ImageCmsProfile(
            ImageCms.createProfile("sRGB")).tobytes())
    result = detect_pointer(out.getvalue(), verified_marker=MARKER)
    assert result is not None
    assert (result.center_x, result.center_y) == (414, 1100)


def test_gray_pointer_touching_blue_rounded_button_border_remains_a_unique_candidate():
    result = detect_pointer(screenshot_cursor_touching_button_border(), verified_marker=MARKER)
    assert result is not None
    assert abs(result.center_x - 241) <= 1
    assert abs(result.center_y - 1295) <= 1
    assert 34 <= result.diameter <= 40


def test_small_blue_seams_do_not_split_one_gray_pointer_into_unknown_fragments():
    result = detect_pointer(screenshot_cursor_cut_by_two_blue_seams(), verified_marker=MARKER)
    assert result is not None
    assert abs(result.center_x - 288) <= 1
    assert abs(result.center_y - 1246) <= 1
    assert 34 <= result.diameter <= 40
