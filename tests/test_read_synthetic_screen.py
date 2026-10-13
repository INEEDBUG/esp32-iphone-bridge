import json
import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from test_visual_marker import fixture as marker_fixture


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_synthetic_screen.ps1"
POWERSHELL = shutil.which("powershell.exe")


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell 5.1 is unavailable")
def test_reader_rejects_non_phonelab_image_without_emitting_ocr_text(tmp_path):
    image_path = tmp_path / "blank.png"
    Image.new("RGB", (128, 128), "white").save(image_path)
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(SCRIPT),
         "-ImagePath", str(image_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode in {0, 2}
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "ERROR"
    assert payload["code"] in {"PAGE_NOT_RECOGNIZED", "OCR_UNAVAILABLE"}
    assert set(payload) == {"status", "code"}
    assert result.stderr == ""


def _make_synthetic_page(path, *, probes, summary, title=True):
    font_path = Path(r"C:\Windows\Fonts\arial.ttf")
    if not font_path.is_file():
        pytest.skip("Arial font unavailable for deterministic OCR fixture")
    image = Image.new("RGB", (1400, 700), "white")
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(font_path), 54)
    body_font = ImageFont.truetype(str(font_path), 48)
    if title:
        draw.text((40, 30), "PhoneLab Synthetic Test Page", font=title_font, fill="black")
    for index, probe in enumerate(probes):
        draw.text((60, 160 + index * 90), f"Probe {probe[0]} ({probe[1]})", font=body_font, fill="black")
    draw.text((60, 500), f"Browser-reported probe clicks: {summary[0]} | Last: {summary[1]}",
              font=body_font, fill="black")
    image.save(path)


def _run_reader(image_path, expected_nonce=None):
    command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(SCRIPT),
               "-ImagePath", str(image_path)]
    if expected_nonce is not None:
        command += ["-ExpectedNonce", expected_nonce]
    return subprocess.run(
        command,
        capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell 5.1 is unavailable")
def test_reader_returns_visible_probe_text_bounds_and_summary(tmp_path):
    image_path = tmp_path / "synthetic.png"
    _make_synthetic_page(image_path, probes=[("06", 1), ("12", 1)], summary=(2, "06"))
    result = _run_reader(image_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "OK"
    assert payload["summary_clicks"] == 2
    assert payload["summary_last"] == "06"
    visible = {item["id"]: item for item in payload["visible_probes"]}
    assert visible["06"]["count"] == 1
    assert visible["12"]["count"] == 1
    for item in visible.values():
        bounds = item["image_text_bounds"]
        assert set(bounds) == {"x", "y", "width", "height"}
        assert bounds["x"] >= 0 and bounds["y"] >= 0
        assert bounds["width"] > 0 and bounds["height"] > 0
    assert "PhoneLab Synthetic Test Page" not in result.stdout
    assert result.stderr == ""


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell 5.1 is unavailable")
def test_reader_rejects_wrong_marker_nonce_before_ocr(tmp_path):
    image_path = tmp_path / "wrong-nonce.png"
    image = Image.open(io.BytesIO(marker_fixture()[0]))
    image.save(image_path)
    result = _run_reader(image_path, expected_nonce="wrong")
    assert result.returncode == 2
    payload = json.loads(result.stdout.strip())
    assert payload == {"status": "ERROR", "code": "MARKER_NOT_VALIDATED"}
    assert result.stderr == ""


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell 5.1 is unavailable")
def test_reader_rejects_blank_image_in_marker_mode(tmp_path):
    image_path = tmp_path / "blank-marker.png"
    Image.new("RGB", (128, 128), "white").save(image_path)
    result = _run_reader(image_path, expected_nonce="hello")
    assert result.returncode == 2
    assert json.loads(result.stdout.strip()) == {
        "status": "ERROR", "code": "MARKER_NOT_VALIDATED"
    }
    assert result.stderr == ""


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell 5.1 is unavailable")
def test_reader_marker_mode_accepts_visible_probe_without_page_labels(tmp_path):
    image_path = tmp_path / "marked-probe-only.png"
    image = Image.open(io.BytesIO(marker_fixture()[0]))
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 48)
    draw.text((40, 40), "Probe 06 (1)", font=font, fill="black")
    image.save(image_path)
    result = _run_reader(image_path, expected_nonce="hello")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "PARTIAL"
    assert payload["code"] == "SUMMARY_NOT_VISIBLE"
    assert payload["summary_clicks"] is None
    assert payload["summary_last"] is None
    visible = {item["id"]: item for item in payload["visible_probes"]}
    assert visible["06"]["count"] == 1
    assert visible["06"]["image_text_bounds"]["width"] > 0
    assert "Probe 06" not in result.stdout
    assert result.stderr == ""


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell 5.1 is unavailable")
def test_reader_supports_initial_last_none_state(tmp_path):
    image_path = tmp_path / "initial.png"
    _make_synthetic_page(image_path, probes=[("06", 0)], summary=(0, "none"))
    result = _run_reader(image_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "OK"
    assert payload["summary_clicks"] == 0
    assert payload["summary_last"] == "none"
    assert payload["visible_probes"][0]["id"] == "06"
    assert payload["visible_probes"][0]["count"] == 0
