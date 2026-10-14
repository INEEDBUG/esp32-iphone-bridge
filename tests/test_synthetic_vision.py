import io

from PIL import Image, ImageCms, ImageDraw, ImageFont
import pytest

import phonelab.synthetic_vision as synthetic_vision
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


def gray_paragraph(*, cursor=True, second_cursor=False):
    image = Image.new("RGB", (828, 1792), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((200, 1073), "reports its own event fields", font=ImageFont.load_default(size=32),
              fill=(105, 109, 116))
    if cursor:
        draw.ellipse((395, 1081, 433, 1119), fill=(160, 162, 165))
    if second_cursor:
        draw.ellipse((600, 1081, 638, 1119), fill=(160, 162, 165))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_gray_pointer_remains_localizable_when_thin_gray_paragraph_strokes_touch_it():
    result = detect_pointer(gray_paragraph(), verified_marker=MARKER)
    assert result is not None
    assert abs(result.center_x - 414) <= 3
    assert abs(result.center_y - 1100) <= 3
    assert 32 <= result.diameter <= 44


def test_removing_gray_text_strokes_does_not_create_a_pointer_or_hide_a_second_one():
    assert detect_pointer(gray_paragraph(cursor=False), verified_marker=MARKER) is None
    assert detect_pointer(gray_paragraph(second_cursor=True), verified_marker=MARKER) is None


def test_neutral_mask_keeps_exact_color_band_and_safe_vertical_boundaries():
    from phonelab.synthetic_vision import _neutral_masks
    colors = [(69, 69, 69), (70, 70, 70), (219, 220, 220), (220, 220, 220),
              (250, 250, 250), (251, 251, 251), (80, 104, 90), (80, 105, 90)]
    image = Image.new("RGB", (len(colors), 1512), (255, 255, 255))
    for y in (159, 160, 1510, 1511):
        for x, color in enumerate(colors):
            image.putpixel((x, y), color)
    dark, light = _neutral_masks(image)
    for y in (160, 1510):
        assert [dark.getpixel((x, y)) for x in range(len(colors))] == [0, 1, 1, 0, 0, 0, 1, 0]
        assert [light.getpixel((x, y)) for x in range(len(colors))] == [0, 0, 0, 1, 1, 0, 0, 0]
    for y in (159, 1511):
        assert not any(dark.getpixel((x, y)) or light.getpixel((x, y)) for x in range(len(colors)))


def _reference_components(raw, width, height):
    from collections import deque

    seen = bytearray(width * height)
    components = []
    for start, active in enumerate(raw):
        if not active or seen[start]:
            continue
        queue = deque([start])
        seen[start] = 1
        min_x = max_x = start % width
        min_y = max_y = start // width
        area = 0
        touches_edge = False
        while queue:
            index = queue.popleft()
            x, y = index % width, index // width
            area += 1
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            if x == 0 or y == 0 or x == width - 1 or y == height - 1:
                touches_edge = True
            for dy in (-1, 0, 1):
                ny = y + dy
                if not 0 <= ny < height:
                    continue
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if (dx == 0 and dy == 0) or not 0 <= nx < width:
                        continue
                    neighbor = ny * width + nx
                    if raw[neighbor] and not seen[neighbor]:
                        seen[neighbor] = 1
                        queue.append(neighbor)
        components.append((area, min_x, min_y, max_x, max_y, touches_edge))
    return sorted(components)


def test_scanline_components_match_8_connected_reference_for_exhaustive_and_random_masks():
    for bits in range(1 << 9):
        raw = bytes((bits >> index) & 1 for index in range(9))
        actual = sorted(synthetic_vision._connected_components(raw, 3, 3))
        assert actual == _reference_components(raw, 3, 3)

    import random

    fixtures = []
    # Degenerate row/column dimensions exercise both scan and boundary limits.
    for width, height in ((1, 1), (1, 29), (31, 1), (2, 23), (27, 2)):
        rng = random.Random(width * 100 + height)
        fixtures.append((width, height, bytes(rng.choice((0, 0, 0, 1, 2, 127, 255))
                                              for _ in range(width * height))))

    # Diagonal-only adjacency and a ring with a hole.
    diagonal = bytearray(11 * 11)
    for i in range(11):
        diagonal[i * 11 + i] = 255
    fixtures.append((11, 11, bytes(diagonal)))
    ring = bytearray(13 * 13)
    for y in range(2, 11):
        for x in range(2, 11):
            if x in (2, 10) or y in (2, 10):
                ring[y * 13 + x] = 1
    fixtures.append((13, 13, bytes(ring)))

    # A rejected edge-connected field beside a compact component must be fully consumed.
    mixed = bytearray(40 * 32)
    for y in range(32):
        for x in range(22):
            mixed[y * 40 + x] = 1
    for y in range(12, 16):
        for x in range(31, 35):
            mixed[y * 40 + x] = 2
    fixtures.append((40, 32, bytes(mixed)))

    for seed in range(100):
        rng = random.Random(seed)
        width, height = rng.randint(1, 37), rng.randint(1, 31)
        raw = bytes(rng.choice((0, 0, 0, 1, 2, 128, 255)) for _ in range(width * height))
        fixtures.append((width, height, raw))

    for width, height, raw in fixtures:
        actual = sorted(synthetic_vision._connected_components(raw, width, height))
        assert actual == _reference_components(raw, width, height)
