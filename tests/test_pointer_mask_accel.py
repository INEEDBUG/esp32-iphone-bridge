import random

from PIL import Image, ImageDraw, ImageFilter
import pytest

import phonelab.synthetic_vision as synthetic_vision
from phonelab.synthetic_vision import detect_pointer
from phonelab.visual_marker import VisualMarker


def _pillow_cleanup(mask):
    result = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    for _ in range(5):
        result = result.filter(ImageFilter.MinFilter(3))
    for _ in range(5):
        result = result.filter(ImageFilter.MaxFilter(3))
    return result


def _mask(width, height, raw):
    return Image.frombytes("L", (width, height), raw)


def _assert_cleanup_matches_pillow(width, height, raw):
    expected = _pillow_cleanup(_mask(width, height, raw))
    actual = synthetic_vision._cleanup_pointer_mask(_mask(width, height, raw))
    assert actual.size == (width, height)
    assert actual.tobytes() == expected.tobytes()


def test_cleanup_matches_pillow_for_all_3x3_binary_masks():
    for bits in range(1 << 9):
        raw = bytes((bits >> index) & 1 for index in range(9))
        _assert_cleanup_matches_pillow(3, 3, raw)


def test_cleanup_matches_pillow_for_varied_edges_degenerate_shapes_and_patterns():
    fixtures = []
    for width, height in ((1, 1), (1, 29), (31, 1), (2, 23), (27, 2),
                          (9, 7), (17, 13), (32, 31), (47, 39)):
        rng = random.Random(width * 100 + height)
        fixtures.append((width, height, bytes(rng.choice((0, 0, 0, 1, 255))
                                              for _ in range(width * height))))

    # Exercise narrow structures and all four replicated image boundaries.
    for width, height in ((19, 15), (23, 11)):
        cases = []
        edge = bytearray(width * height)
        for x in range(width):
            edge[x] = 1
            edge[(height - 1) * width + x] = 1
        for y in range(height):
            edge[y * width] = 1
            edge[y * width + width - 1] = 1
        cases.append(bytes(edge))

        ring = bytearray(width * height)
        for y in range(2, height - 2):
            for x in range(2, width - 2):
                if x in (2, width - 3) or y in (2, height - 3):
                    ring[y * width + x] = 1
        cases.append(bytes(ring))

        stripes = bytearray(width * height)
        for y in range(height):
            for x in range(width):
                if x % 4 == 0 or y % 5 == 0:
                    stripes[y * width + x] = 255
        cases.append(bytes(stripes))
        fixtures.extend((width, height, raw) for raw in cases)

    for seed in range(40):
        rng = random.Random(seed)
        width, height = rng.randint(1, 41), rng.randint(1, 37)
        raw = bytes(rng.choice((0, 0, 0, 1, 7, 128, 255))
                    for _ in range(width * height))
        fixtures.append((width, height, raw))

    for width, height, raw in fixtures:
        _assert_cleanup_matches_pillow(width, height, raw)


def test_missing_optional_opencv_uses_exact_pillow_cleanup(monkeypatch):
    monkeypatch.setattr(synthetic_vision, "cv2", None, raising=False)
    monkeypatch.setattr(synthetic_vision, "np", None, raising=False)
    width, height = 29, 21
    rng = random.Random(712)
    raw = bytes(rng.choice((0, 0, 1, 255)) for _ in range(width * height))
    _assert_cleanup_matches_pillow(width, height, raw)


def test_accelerator_runtime_errors_are_not_silently_replaced_by_fallback(monkeypatch):
    cv2 = getattr(synthetic_vision, "cv2", None)
    if cv2 is None:
        pytest.skip("OpenCV is not installed in this environment")

    def fail(*args, **kwargs):
        raise RuntimeError("simulated morphology failure")

    monkeypatch.setattr(cv2, "morphologyEx", fail)
    with pytest.raises(RuntimeError, match="simulated morphology failure"):
        synthetic_vision._cleanup_pointer_mask(Image.new("L", (9, 9), 0))


def _pointer_pngs():
    cases = []
    for centers in (((414, 1100),), ((300, 1100), (500, 1100)), ((8, 1100),)):
        image = Image.new("RGB", (828, 1792), (245, 245, 245))
        draw = ImageDraw.Draw(image)
        for center in centers:
            x, y = center
            draw.ellipse((x - 18, y - 18, x + 18, y + 18), fill=(110, 110, 110))
        import io

        output = io.BytesIO()
        image.save(output, format="PNG")
        cases.append(output.getvalue())
    return cases


def test_pointer_detector_outputs_match_pillow_fallback_for_candidate_and_rejections(monkeypatch):
    cv2 = getattr(synthetic_vision, "cv2", None)
    np = getattr(synthetic_vision, "np", None)
    if cv2 is None or np is None:
        pytest.skip("OpenCV and NumPy are not installed in this environment")

    marker = VisualMarker(counter=7, bounds=(728, 1692, 80, 80), pixels_per_css_pixel=2.0)
    for png in _pointer_pngs():
        accelerated = detect_pointer(png, verified_marker=marker)
        monkeypatch.setattr(synthetic_vision, "cv2", None)
        monkeypatch.setattr(synthetic_vision, "np", None)
        reference = detect_pointer(png, verified_marker=marker)
        monkeypatch.setattr(synthetic_vision, "cv2", cv2)
        monkeypatch.setattr(synthetic_vision, "np", np)
        assert accelerated == reference

    assert detect_pointer(_pointer_pngs()[0], verified_marker=marker) is not None
    assert detect_pointer(_pointer_pngs()[1], verified_marker=marker) is None
    assert detect_pointer(_pointer_pngs()[2], verified_marker=marker) is None


def test_existing_synthetic_pointer_fixtures_match_pillow_fallback(monkeypatch):
    cv2 = getattr(synthetic_vision, "cv2", None)
    np = getattr(synthetic_vision, "np", None)
    if cv2 is None or np is None:
        pytest.skip("OpenCV and NumPy are not installed in this environment")

    from test_synthetic_vision import (
        MARKER,
        gray_paragraph,
        screenshot,
        screenshot_cursor_cut_by_two_blue_seams,
        screenshot_cursor_touching_button_border,
    )

    fixtures = [
        screenshot(cursor=((414, 1100), 18, (110, 110, 110))),
        screenshot(cursor=((414, 1100), 18, (235, 235, 235)), background=(120, 120, 120)),
        screenshot(extra=(((80, 250), 60, (185, 187, 190)),)),
        screenshot_cursor_touching_button_border(),
        screenshot_cursor_cut_by_two_blue_seams(),
        gray_paragraph(),
        gray_paragraph(cursor=False),
        gray_paragraph(second_cursor=True),
    ]

    for png in fixtures:
        accelerated = detect_pointer(png, verified_marker=MARKER)
        with monkeypatch.context() as fallback:
            fallback.setattr(synthetic_vision, "cv2", None)
            fallback.setattr(synthetic_vision, "np", None)
            reference = detect_pointer(png, verified_marker=MARKER)
        assert accelerated == reference
