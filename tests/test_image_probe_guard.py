import binascii
import io

from PIL import Image, ImageDraw
import pytest

from phonelab.image_probe_guard import ImageProbeGuard


NONCE = "synthetic-run"


def marker_rgb(nonce):
    value = 2166136261
    for byte in nonce.encode("ascii"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    payload = value.to_bytes(4, "big") + (9).to_bytes(2, "big")
    payload += binascii.crc_hqx(payload, 0xFFFF).to_bytes(2, "big")
    colors = []
    for row in range(10):
        line = []
        for col in range(10):
            if row == 0: color = (0, 200, 220)
            elif row == 9: color = (220, 0, 200)
            elif col == 0: color = (250, 200, 0)
            elif col == 9: color = (30, 80, 240)
            else:
                bit = (row - 1) * 8 + col - 1
                color = (20, 20, 20) if payload[bit // 8] & (1 << (7 - bit % 8)) else (250, 250, 250)
            line.append(color)
        colors.append(line)
    return colors


def page(*, label="Probe 07 (0)", button=(300, 400, 520, 500), marker=True):
    image = Image.new("RGB", (828, 1792), (239, 242, 247))
    draw = ImageDraw.Draw(image)
    x1, y1, x2, y2 = button
    draw.rounded_rectangle(button, radius=18, fill=(237, 244, 255), outline=(40, 94, 200), width=2)
    draw.text((x1 + 60, y1 + 36), label, fill=(18, 59, 132))
    if marker:
        cells = marker_rgb(NONCE)
        for row, line in enumerate(cells):
            for col, color in enumerate(line):
                draw.rectangle((728 + col * 8, 1600 + row * 8,
                                735 + col * 8, 1607 + row * 8), fill=color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def ocr(label="Probe 07 (0)", bounds=None):
    if bounds is None:
        bounds = {"x": 360, "y": 436, "width": 100, "height": 20}
    return {"status": "OK", "visible_probes": [
        {"id": "07", "count": 0, "image_text": label, "image_text_bounds": bounds}]}


def pointer(_png, *, verified_marker):
    return type("Pointer", (), {"center_x": 410.0, "center_y": 478.0, "diameter": 38.0})()


def test_valid_image_guard_accepts_only_target_inside_unchanged_button(monkeypatch):
    import phonelab.image_probe_guard as module
    monkeypatch.setattr(module, "detect_pointer", pointer)
    guard = ImageProbeGuard("07", page(), NONCE, ocr())
    result = guard.verify(page())
    assert result["probe_id"] == "07"
    assert result["pixel_error"] == 0
    assert result["button_bounds"] == (300, 400, 521, 501)
    assert result["lock_state"] == "UNKNOWN"
    assert result["input_effect_verified"] is False


def test_wrong_nonce_marker_or_missing_marker_rejected(monkeypatch):
    import phonelab.image_probe_guard as module
    with pytest.raises(ValueError):
        ImageProbeGuard("07", page(), "wrong-run", ocr())


def test_wrong_probe_missing_or_ambiguous_ocr_rejected(monkeypatch):
    import phonelab.image_probe_guard as module
    for probe_id, fields in [("00", ocr()), ("07", {"status": "OK", "visible_probes": []}),
                             ("07", {"status": "OK", "visible_probes": [ocr()["visible_probes"][0]] * 2})]:
        with pytest.raises(ValueError):
            ImageProbeGuard(probe_id, page(), NONCE, fields)


def test_shifted_button_and_changed_label_pixels_rejected(monkeypatch):
    import phonelab.image_probe_guard as module
    monkeypatch.setattr(module, "detect_pointer", pointer)
    guard = ImageProbeGuard("07", page(), NONCE, ocr())
    with pytest.raises(ValueError):
        guard.verify(page(button=(320, 400, 540, 500)))
    with pytest.raises(ValueError):
        guard.verify(page(label="Probe 08 (0)"))


def test_pointer_must_be_unique_near_goal_and_inside_button_with_margin(monkeypatch):
    import phonelab.image_probe_guard as module
    monkeypatch.setattr(module, "detect_pointer", pointer)
    guard = ImageProbeGuard("07", page(), NONCE, ocr())
    monkeypatch.setattr(module, "detect_pointer", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError):
        guard.verify(page())
    monkeypatch.setattr(module, "detect_pointer", lambda *_args, **_kwargs:
                        type("P", (), {"center_x": 410., "center_y": 478., "diameter": 38.})())
    assert guard.verify(page())["probe_id"] == "07"
    monkeypatch.setattr(module, "detect_pointer", lambda *_args, **_kwargs:
                        type("P", (), {"center_x": 422., "center_y": 478., "diameter": 38.})())
    with pytest.raises(ValueError):
        guard.verify(page())
    monkeypatch.setattr(module, "detect_pointer", lambda *_args, **_kwargs:
                        type("P", (), {"center_x": 410., "center_y": 445., "diameter": 38.})())
    with pytest.raises(ValueError):
        guard.verify(page())


def test_partial_toolbar_or_ambiguous_blue_buttons_rejected(monkeypatch):
    import phonelab.image_probe_guard as module
    monkeypatch.setattr(module, "detect_pointer", pointer)
    with pytest.raises(ValueError):
        ImageProbeGuard("07", page(button=(300, 1400, 520, 1500)), NONCE,
                        ocr(bounds={"x": 360, "y": 1436, "width": 100, "height": 20}))
    ambiguous = Image.new("RGB", (828, 1792), (239, 242, 247))
    draw = ImageDraw.Draw(ambiguous)
    draw.rounded_rectangle((280, 380, 540, 520), radius=18, fill=(237, 244, 255), outline=(40, 94, 200), width=2)
    draw.rounded_rectangle((300, 400, 524, 504), radius=18, fill=(237, 244, 255), outline=(40, 94, 200), width=2)
    draw.text((360, 436), "Probe 07 (0)", fill=(18, 59, 132))
    cells = marker_rgb(NONCE)
    for row, line in enumerate(cells):
        for col, color in enumerate(line):
            draw.rectangle((728 + col * 8, 1600 + row * 8,
                            735 + col * 8, 1607 + row * 8), fill=color)
    data = io.BytesIO(); ambiguous.save(data, format="PNG")
    with pytest.raises(ValueError):
        ImageProbeGuard("07", data.getvalue(), NONCE, ocr())








def test_sanitized_ocr_schema_does_not_require_unreturned_text(monkeypatch):
    import phonelab.image_probe_guard as module
    monkeypatch.setattr(module, "detect_pointer", pointer)
    fields = {"status": "OK", "visible_probes": [
        {"id": "07", "count": 0,
         "image_text_bounds": {"x": 360, "y": 436, "width": 100, "height": 20}}]}
    guard = ImageProbeGuard("07", page(), NONCE, fields)
    assert guard.verify(page())["probe_id"] == "07"


def test_row_column_outline_fallback_separates_adjacent_merged_buttons(monkeypatch):
    import phonelab.image_probe_guard as module
    monkeypatch.setattr(module, "detect_pointer", lambda *_args, **_kwargs:
                        type("P", (), {"center_x": 410., "center_y": 500., "diameter": 32.})())
    image = Image.new("RGB", (828, 1792), (239, 242, 247))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((300, 400, 520, 500), radius=18,
                           fill=(237, 244, 255), outline=(40, 94, 200), width=2)
    draw.rounded_rectangle((300, 501, 520, 601), radius=18,
                           fill=(237, 244, 255), outline=(40, 94, 200), width=2)
    draw.ellipse((396, 486, 424, 514), fill=(180, 180, 180))
    draw.text((360, 436), "Probe 07 (0)", fill=(18, 59, 132))
    for row, line in enumerate(marker_rgb(NONCE)):
        for col, color in enumerate(line):
            draw.rectangle((728 + col * 8, 1600 + row * 8,
                            735 + col * 8, 1607 + row * 8), fill=color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    fields = {"status": "OK", "visible_probes": [
        {"id": "07", "count": 0,
         "image_text_bounds": {"x": 360, "y": 436, "width": 100, "height": 20}}]}
    guard = ImageProbeGuard("07", stream.getvalue(), NONCE, fields)
    assert guard._button[1] < 501
