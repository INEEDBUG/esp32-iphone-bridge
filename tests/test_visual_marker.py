"""Synthetic pixel checks; no phone, lock-state, or input proof."""
import binascii
import hashlib
import io

from PIL import Image, ImageCms, ImageDraw
import pytest

from phonelab.visual_marker import MarkerError, VisualMarkerTracker, decode_visual_marker


def fixture(counter=0, *, scale=2, nonce_hash=0x4F9F2CAB, corrupt=False):
    # FNV-1a32("hello") is a published fixed vector, independent of the decoder.
    payload = nonce_hash.to_bytes(4, "big") + counter.to_bytes(2, "big")
    payload += binascii.crc_hqx(payload, 0xFFFF).to_bytes(2, "big")
    if corrupt:
        payload = payload[:-1] + bytes([payload[-1] ^ 1])
    image = Image.new("RGB", (828, 1792), (239, 242, 247))
    draw = ImageDraw.Draw(image)
    left, top, cell = 828 - 100 * scale, 1792 - 100 * scale, 4 * scale
    for row in range(10):
        for col in range(10):
            if row == 0: color = (0, 200, 220)
            elif row == 9: color = (220, 0, 200)
            elif col == 0: color = (250, 200, 0)
            elif col == 9: color = (30, 80, 240)
            else:
                bit = (row - 1) * 8 + col - 1
                color = (20, 20, 20) if payload[bit // 8] & (1 << (7 - bit % 8)) else (250, 250, 250)
            x, y = left + col * cell, top + row * cell
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), (left, top, cell * 10, cell * 10)


def _with_icc(png, profile_bytes):
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.save(out, format="PNG", icc_profile=profile_bytes)
    return out.getvalue()


def _p3_cyan_simulation(png):
    """Synthesize the observed P3-encoded cyan without any device image data."""
    with Image.open(io.BytesIO(png)) as image:
        image = image.convert("RGB")
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                if pixels[x, y] == (0, 200, 220):
                    pixels[x, y] = (90, 197, 217)
        out = io.BytesIO()
        image.save(out, format="PNG", icc_profile=ImageCms.ImageCmsProfile(
            ImageCms.createProfile("sRGB")).tobytes())
    return out.getvalue()


@pytest.mark.parametrize("scale", [1, 2, 3])
def test_decodes_nonce_counter_and_pixel_bounds_from_synthetic_phone_sized_png(scale):
    png, bounds = fixture(1234, scale=scale)
    marker = decode_visual_marker(png, "hello")
    assert marker.counter == 1234
    assert marker.bounds == bounds
    assert marker.pixels_per_css_pixel == scale


def test_embedded_icc_profile_is_converted_to_srgb_before_marker_thresholds(monkeypatch):
    png, bounds = fixture(193)
    p3_encoded = _p3_cyan_simulation(png)
    profile_calls = []
    expected, _ = fixture(193)
    with Image.open(io.BytesIO(expected)) as opened:
        normalized = opened.convert("RGB")

    def fake_profile_to_profile(image, source, destination, *, outputMode):
        profile_calls.append((source, destination, outputMode))
        return normalized

    monkeypatch.setattr("phonelab.visual_marker.ImageCms", ImageCms, raising=False)
    monkeypatch.setattr(ImageCms, "profileToProfile", fake_profile_to_profile)
    marker = decode_visual_marker(p3_encoded, "hello")
    assert marker.counter == 193
    assert marker.bounds == bounds
    assert len(profile_calls) == 1
    assert profile_calls[0][2] == "RGB"


def test_valid_embedded_srgb_profile_uses_real_pillow_conversion():
    png, bounds = fixture(29)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    marker = decode_visual_marker(_with_icc(png, profile), "hello")
    assert marker.counter == 29
    assert marker.bounds == bounds


def test_invalid_embedded_icc_profile_is_rejected():
    png, _ = fixture()
    damaged = _with_icc(png, b"not a valid ICC profile")
    with pytest.raises(MarkerError):
        decode_visual_marker(damaged, "hello")


def test_oversized_embedded_icc_profile_is_rejected():
    png, _ = fixture()
    oversized = _with_icc(png, hashlib.shake_256(b"synthetic oversized ICC profile").digest(600 * 1024))
    with pytest.raises(MarkerError):
        decode_visual_marker(oversized, "hello")


@pytest.mark.parametrize("kwargs", [{"corrupt": True}, {"nonce_hash": 0x11111111}])
def test_corrupt_or_wrong_run_marker_is_rejected(kwargs):
    png, _ = fixture(**kwargs)
    with pytest.raises(MarkerError):
        decode_visual_marker(png, "hello")


def test_missing_occluded_or_rotated_marker_is_rejected():
    raw, bounds = fixture()
    with Image.open(io.BytesIO(raw)) as image:
        draw = ImageDraw.Draw(image)
        x, y, width, height = bounds
        draw.rectangle((x, y, x + width, y + height), fill=(255, 255, 255))
        out = io.BytesIO()
        image.save(out, format="PNG")
    with pytest.raises(MarkerError):
        decode_visual_marker(out.getvalue(), "hello")
    with Image.open(io.BytesIO(raw)) as image:
        rotated = image.rotate(90, expand=True)
        out = io.BytesIO()
        rotated.save(out, format="PNG")
    with pytest.raises(MarkerError):
        decode_visual_marker(out.getvalue(), "hello")


def test_static_content_and_repeated_http_reads_do_not_establish_or_renew_progress():
    tracker = VisualMarkerTracker("hello")
    zero, _ = fixture(0)
    one, _ = fixture(1)
    assert tracker.observe(zero, sequence=1, received=10., now=10.)["state"] == "pending_progress"
    assert tracker.observe(zero, sequence=2, received=10.5, now=10.5)["state"] == "pending_progress"
    assert tracker.observe(one, sequence=3, received=11., now=11.)["state"] == "progress_observed"
    assert tracker.observe(one, sequence=3, received=11., now=12.)["state"] == "progress_observed"
    assert tracker.observe(one, sequence=4, received=12.6, now=12.6)["state"] == "stale_content"


@pytest.mark.parametrize("sequence,received,now", [(0, 10., 10.), (1, 7., 10.), (1, 11., 10.)])
def test_invalid_sequence_and_old_or_future_capture_receipts_fail_closed(sequence, received, now):
    tracker = VisualMarkerTracker("hello")
    raw, _ = fixture()
    with pytest.raises(MarkerError):
        tracker.observe(raw, sequence=sequence, received=received, now=now)


def test_counter_reset_frame_reset_and_decode_failure_invalidate_previous_progress():
    tracker = VisualMarkerTracker("hello")
    zero, _ = fixture(0)
    one, _ = fixture(1)
    tracker.observe(zero, sequence=1, received=10., now=10.)
    assert tracker.observe(one, sequence=2, received=10.5, now=10.5)["state"] == "progress_observed"
    with pytest.raises(MarkerError):
        tracker.observe(zero, sequence=3, received=11., now=11.)
    assert tracker.observe(zero, sequence=4, received=11.5, now=11.5)["state"] == "pending_progress"
    with pytest.raises(MarkerError):
        tracker.observe(one, sequence=2, received=12., now=12.)


def test_progress_is_only_pixel_observation_and_never_lock_or_hardware_proof():
    tracker = VisualMarkerTracker("hello")
    zero, _ = fixture(0)
    one, _ = fixture(1)
    tracker.observe(zero, sequence=1, received=10., now=10.)
    result = tracker.observe(one, sequence=2, received=10.5, now=10.5)
    assert result["lock_state"] == "UNKNOWN"
    assert result["input_verified"] is False
    assert result["source_time_available"] is False


@pytest.mark.parametrize("counter", [0, 1, 301])
def test_javascript_page_raster_and_python_decoder_interoperate(counter):
    from test_lab_page import render_marker_rgb
    nonce = "synthetic-run-7f31"
    cells = render_marker_rgb(nonce, counter)
    raster = Image.new("RGB", (10, 10))
    raster.putdata([tuple(color) for row in cells for color in row])
    image = Image.new("RGB", (828, 1792), (239, 242, 247))
    image.paste(raster.resize((80, 80), Image.Resampling.NEAREST), (708, 1612))
    out = io.BytesIO()
    image.save(out, format="PNG")
    marker = decode_visual_marker(out.getvalue(), nonce)
    assert marker.counter == counter
    assert marker.bounds == (708, 1612, 80, 80)
