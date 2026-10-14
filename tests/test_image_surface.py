import io

from PIL import Image, ImageDraw
import pytest

from test_visual_marker import fixture as marker_fixture
from phonelab.image_surface import find_surface


COLORS = {
    "text": ((232, 255, 244), (100, 116, 139), (0, 133, 119)),
    "drag-source": ((255, 242, 212), (180, 100, 20), None),
    "drop-target": ((242, 248, 237), (70, 130, 60), None),
    "native-scroll": ((236, 242, 250), (65, 85, 110), None),
}
BOX = (120, 350, 420, 470)
NONCE = "hello"


def _png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _page(*, role="text", focused=False, boxes=None, missing_sides=(), mixed=False,
          holes=False, fill_adjust=(0, 0, 0), border_adjust=(0, 0, 0)):
    png, _ = marker_fixture()
    marked = Image.open(io.BytesIO(png)).convert("RGB")
    image = Image.new("RGB", (828, 1792), (243, 246, 250))
    image.paste(marked.crop((508, 1312, 828, 1792)), (508, 1312))
    draw = ImageDraw.Draw(image)
    fill, inactive, active = COLORS[role]
    active = active or inactive
    fill = tuple(value + change for value, change in zip(fill, fill_adjust))
    inactive = tuple(value + change for value, change in zip(inactive, border_adjust))
    active = tuple(value + change for value, change in zip(active, border_adjust))
    boxes = list(boxes if boxes is not None else [BOX])
    for box in boxes:
        left, top, right, bottom = box
        draw.rectangle((left, top, right - 1, bottom - 1), fill=fill)
        if holes:
            draw.rectangle((left + 25, top + 25, left + 34, top + 34), fill=(10, 10, 10))
            draw.rectangle((left + 70, top + 50, left + 73, top + 70), fill=(20, 30, 40))
        side_colors = [active if focused else inactive] * 4
        if mixed:
            side_colors[0] = active
            side_colors[1:] = [inactive] * 3
        side_coordinates = (
            [(x, y) for x in range(left - 1, left - 3, -1)
             for y in range(top + 30, bottom - 30)],
            [(x, y) for x in range(right, right + 2)
             for y in range(top + 30, bottom - 30)],
            [(x, y) for y in range(top - 1, top - 3, -1)
             for x in range(left + 75, right - 75)],
            [(x, y) for y in range(bottom, bottom + 2)
             for x in range(left + 75, right - 75)],
        )
        for side, (color, coordinates) in enumerate(zip(side_colors, side_coordinates)):
            for point in coordinates:
                if side not in missing_sides:
                    draw.point(point, fill=color)
    return _png(image)


@pytest.mark.parametrize("role", list(COLORS))
def test_finds_one_fully_bordered_surface_and_returns_only_geometry(role):
    result = find_surface(_page(role=role), NONCE, role)
    assert result == {
        "role": role,
        "bounds": BOX,
        "focused": False if role == "text" else None,
    }
    assert set(result) == {"role", "bounds", "focused"}


def test_text_surface_distinguishes_uniform_focused_border():
    assert find_surface(_page(focused=True), NONCE, "text") == {
        "role": "text", "bounds": BOX, "focused": True,
    }


def test_connected_fill_with_small_pointer_and_glyph_holes_remains_one_surface():
    assert find_surface(_page(holes=True), NONCE, "text")["bounds"] == BOX


def test_fill_and_border_accept_per_channel_color_variation_of_three():
    assert find_surface(_page(fill_adjust=(3, -3, 3), border_adjust=(-3, 3, -3)),
                        NONCE, "text")["bounds"] == BOX


def test_fill_outside_per_channel_color_tolerance_is_not_a_surface():
    with pytest.raises(ValueError):
        find_surface(_page(fill_adjust=(4, 0, 0)), NONCE, "text")


@pytest.mark.parametrize("case", ["duplicate", "missing-side", "mixed-border", "clipped"])
def test_refuses_duplicate_or_incomplete_surface_geometry(case):
    if case == "duplicate":
        png = _page(boxes=[BOX, (500, 600, 800, 720)])
    elif case == "missing-side":
        png = _page(missing_sides=(0,))
    elif case == "mixed-border":
        png = _page(mixed=True)
    else:
        png = _page(boxes=[(0, 350, 300, 470)])
    with pytest.raises(ValueError):
        find_surface(png, NONCE, "text")


@pytest.mark.parametrize("role", ["text", "drag-source", "drop-target", "native-scroll"])
def test_rejects_wrong_marker_nonce(role):
    with pytest.raises(ValueError):
        find_surface(_page(role=role), "different-run", role)


def test_rejects_non_phone_lab_image_size_before_geometry_detection():
    png, _ = marker_fixture()
    image = Image.open(io.BytesIO(png)).resize((827, 1792))
    with pytest.raises(ValueError):
        find_surface(_png(image), NONCE, "text")


@pytest.mark.parametrize("box", [(5, 350, 305, 470), (120, 170, 420, 290),
                                  (120, 1400, 420, 1520)])
def test_rejects_surface_outside_safe_screen_bounds(box):
    with pytest.raises(ValueError):
        find_surface(_page(boxes=[box]), NONCE, "text")


def test_rejects_unknown_role():
    with pytest.raises(ValueError):
        find_surface(_page(), NONCE, "button")
