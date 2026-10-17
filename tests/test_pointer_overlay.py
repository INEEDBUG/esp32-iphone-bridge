import io
import pytest
from PIL import Image, ImageDraw

from phonelab.pointer_overlay import find_pointer_overlay


def png(image):
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('background', [(220, 35, 20), (25, 40, 180), (245, 245, 245)])
def test_translucent_cursor_on_colored_background(background):
    baseline = Image.new('RGB', (828, 1792), background)
    ImageDraw.Draw(baseline).text((65, 75), 'Text', fill='black')
    overlay = Image.new('RGBA', baseline.size)
    ImageDraw.Draw(overlay).ellipse((70, 70, 105, 105), fill=(70, 70, 70, 150))
    current = Image.alpha_composite(baseline.convert('RGBA'), overlay).convert('RGB')
    result = find_pointer_overlay(png(current), png(baseline), search_bounds=(40, 40, 140, 140))
    assert result is not None
    assert result.center_x == pytest.approx(87.5, abs=1)
    assert result.center_y == pytest.approx(87.5, abs=1)


def test_unchanged_scene_and_large_scene_change_have_no_pointer():
    baseline = Image.new('RGB', (828, 1792), 'red')
    assert find_pointer_overlay(png(baseline), png(baseline), search_bounds=(0, 0, 180, 180)) is None
    current = baseline.copy()
    ImageDraw.Draw(current).rectangle((20, 20, 140, 140), fill='blue')
    assert find_pointer_overlay(png(current), png(baseline), search_bounds=(0, 0, 180, 180)) is None


def test_two_candidates_are_ambiguous():
    baseline = Image.new('RGB', (828, 1792), 'red')
    current = baseline.copy()
    draw = ImageDraw.Draw(current)
    draw.ellipse((20, 20, 55, 55), fill='gray')
    draw.ellipse((100, 100, 135, 135), fill='gray')
    assert find_pointer_overlay(png(current), png(baseline), search_bounds=(0, 0, 180, 180)) is None


def test_circle_with_an_unrelated_large_change_is_not_a_stable_roi():
    baseline = Image.new('RGB', (828, 1792), 'red')
    current = baseline.copy()
    draw = ImageDraw.Draw(current)
    draw.ellipse((20, 20, 55, 55), fill='gray')
    draw.rectangle((90, 90, 160, 160), fill='blue')
    assert find_pointer_overlay(png(current), png(baseline), search_bounds=(0, 0, 180, 180)) is None


def test_mismatched_images_and_invalid_bounds_are_rejected():
    baseline = png(Image.new('RGB', (828, 1792), 'red'))
    with pytest.raises(ValueError):
        find_pointer_overlay(png(Image.new('RGB', (100, 100))), baseline, search_bounds=(0, 0, 90, 90))
    with pytest.raises(ValueError):
        find_pointer_overlay(baseline, baseline, search_bounds=(-1, 0, 90, 90))
