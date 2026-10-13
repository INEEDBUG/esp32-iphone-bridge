import json
import struct
import tempfile
import zlib
from pathlib import Path

import pytest

from phonelab.live_observation import LiveObservationClient, ObservationError, ScreenSnapshot


TARGET = "iphone-ac39233f6b70"
TOKEN = "synthetic-control-token-never-persist"
RUN_ID = "0123456789abcdef"


def png_bytes(width=828, height=1792):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + (b"\x00\x00\x00" * width)
    pixels = zlib.compress(row * height)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def screen_metadata(**changes):
    result = {
        "state": "fresh", "target": TARGET, "sequence": 21, "width": 828, "height": 1792,
        "orientation": "portrait", "source": "USB_SCREENSHOT", "source_time": None,
        "received_monotonic": 99.5, "locked": "UNKNOWN", "fresh": True,
    }
    result.update(changes)
    return result


def run_record(**changes):
    result = {"run_id": RUN_ID, "test_id": "smoke", "seed": "7", "nonce": "synthetic-nonce",
              "last_seq": 1, "phone_verified": False}
    result.update(changes)
    return result


def event_row(**changes):
    result = {"recorded_at": "2026-10-13T12:00:00+00:00", "recorded_monotonic": 99.4,
              "source": "client_reported_browser_event", "run_id": RUN_ID, "test_id": "smoke",
              "seed": "7", "seq": 1,
              "event": {"type": "pointermove", "isTrusted": True, "target": "body",
                        "timeStamp": 12, "clientX": 11, "clientY": 22, "button": -1}}
    result.update(changes)
    return result


class FakeResponse:
    status = 200

    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]


class FakeOpener:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        return self.routes[request.full_url]


def screen_client(tmp_path, *, metadata=None, image=None, now=100.0, opener=None, target=TARGET):
    frame = image if image is not None else png_bytes()
    meta = metadata if metadata is not None else screen_metadata()
    fake = opener or FakeOpener({"http://127.0.0.1:8766/screen": FakeResponse(
        frame, {"X-PhoneLab-Frame": json.dumps(meta)})})
    client = LiveObservationClient("http://127.0.0.1:8766", TOKEN, target,
                                  opener=fake, clock=lambda: now)
    return client, fake


def test_snapshot_authenticates_once_and_keeps_png_and_metadata_from_same_response(tmp_path):
    client, opener = screen_client(tmp_path)
    snapshot = client.snapshot()
    assert snapshot.image_bytes == png_bytes()
    assert snapshot.metadata["sequence"] == 21
    assert snapshot.metadata["source"] == "USB_SCREENSHOT"
    assert snapshot.metadata["locked"] == "UNKNOWN"
    assert snapshot.age_seconds == pytest.approx(0.5)
    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    assert request.full_url == "http://127.0.0.1:8766/screen"
    assert request.get_header("Origin") == "http://127.0.0.1:8766"
    assert request.get_header("X-phonelab-control") == TOKEN
    assert timeout == 3.0
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(snapshot)


@pytest.mark.parametrize("metadata", [
    screen_metadata(target="iphone-000000000000"),
    screen_metadata(state="stale", fresh=False),
    screen_metadata(fresh=False),
    screen_metadata(orientation="landscape"),
    screen_metadata(sequence=0),
    screen_metadata(received_monotonic=100.1),
])
def test_snapshot_rejects_bad_target_state_orientation_sequence_and_receipt(tmp_path, metadata):
    client, _ = screen_client(tmp_path, metadata=metadata)
    with pytest.raises(ObservationError):
        client.snapshot()


def test_snapshot_rejects_stale_frame_and_png_dimension_mismatch(tmp_path):
    stale, _ = screen_client(tmp_path, metadata=screen_metadata(received_monotonic=97.9))
    with pytest.raises(ObservationError, match="stale"):
        stale.snapshot()
    mismatch, _ = screen_client(tmp_path, image=png_bytes(827, 1792))
    with pytest.raises(ObservationError, match="dimensions"):
        mismatch.snapshot()


def test_non_loopback_endpoint_and_git_evidence_root_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="fixed to loopback"):
        LiveObservationClient("http://192.168.1.5:8766", TOKEN, TARGET, opener=lambda *_a, **_k: None)
    repo_root = Path(__file__).resolve().parents[1]
    client = LiveObservationClient("http://127.0.0.1:8766", TOKEN, TARGET,
                                   opener=lambda *_a, **_k: None, clock=lambda: 0)
    snapshot = ScreenSnapshot(png_bytes(), screen_metadata(), 0.5)
    with pytest.raises(ValueError, match="outside the source repository"):
        client.save_snapshot(snapshot, repo_root / "private-test-output", "capture")


def test_request_failure_is_coarse_and_never_echoes_token(tmp_path):
    def failing_opener(*_args, **_kwargs):
        raise RuntimeError(f"synthetic failure containing {TOKEN}")

    client = LiveObservationClient("http://127.0.0.1:8766", TOKEN, TARGET,
                                   opener=failing_opener, clock=lambda: 100)
    with pytest.raises(ObservationError) as error:
        client.snapshot()
    assert TOKEN not in str(error.value)
    assert TOKEN not in repr(error.value)
    assert TOKEN not in repr(client)


def test_save_snapshot_requires_explicit_private_root_and_never_writes_token(tmp_path):
    client, _ = screen_client(tmp_path)
    snapshot = client.snapshot()
    output = client.save_snapshot(snapshot, tmp_path / "private-evidence", "frame-001")
    assert (output / "screen.png").read_bytes() == snapshot.image_bytes
    metadata = (output / "metadata.json").read_text(encoding="utf-8")
    assert "USB_SCREENSHOT" in metadata
    assert TOKEN not in metadata


def test_runs_and_event_rows_are_schema_validated_and_run_scoped(tmp_path):
    listing = {"source": "synthetic_page_observation", "runs": [run_record()]}
    detail = {"source": "synthetic_page_observation", "run": run_record(), "events": [event_row()]}
    opener = FakeOpener({
        "http://127.0.0.1:8766/observations": FakeResponse(json.dumps(listing).encode()),
        f"http://127.0.0.1:8766/observations?run_id={RUN_ID}": FakeResponse(json.dumps(detail).encode()),
    })
    client = LiveObservationClient("http://127.0.0.1:8766", TOKEN, TARGET, opener=opener, clock=lambda: 100)
    assert client.runs() == [run_record()]
    events = client.events(RUN_ID)
    assert events == {"run": run_record(), "events": [event_row()]}
    assert len(opener.requests) == 2
    assert all(request.get_header("X-phonelab-control") == TOKEN for request, _ in opener.requests)
    assert TOKEN not in repr(events)


def test_malformed_observation_run_or_event_sequence_is_rejected():
    bad_listing = {"source": "synthetic_page_observation", "runs": [run_record(phone_verified=True)]}
    opener = FakeOpener({"http://127.0.0.1:8766/observations": FakeResponse(json.dumps(bad_listing).encode())})
    client = LiveObservationClient("http://127.0.0.1:8766", TOKEN, TARGET, opener=opener, clock=lambda: 100)
    with pytest.raises(ObservationError):
        client.runs()

    detail = {"source": "synthetic_page_observation", "run": run_record(),
              "events": [event_row(), event_row(seq=1)]}
    opener = FakeOpener({f"http://127.0.0.1:8766/observations?run_id={RUN_ID}":
                         FakeResponse(json.dumps(detail).encode())})
    client = LiveObservationClient("http://127.0.0.1:8766", TOKEN, TARGET, opener=opener, clock=lambda: 100)
    with pytest.raises(ObservationError):
        client.events(RUN_ID)


def test_multiple_refreshes_select_only_the_run_visible_in_actual_atomic_pixels(tmp_path):
    from test_visual_marker import fixture
    image, _ = fixture(7)
    matching = run_record(nonce='hello')
    stale = run_record(run_id='abcdef0123456789', nonce='other')
    opener = FakeOpener({
        'http://127.0.0.1:8766/screen': FakeResponse(image, {'X-PhoneLab-Frame': json.dumps(screen_metadata())}),
        'http://127.0.0.1:8766/observations': FakeResponse(json.dumps({
            'source':'synthetic_page_observation', 'runs':[stale, matching]}).encode())})
    client = LiveObservationClient('http://127.0.0.1:8766', TOKEN, TARGET, opener=opener, clock=lambda:100)
    assert client.visible_run(test_id='smoke', seed='7') == matching
    with pytest.raises(ObservationError): client.visible_run(test_id='other', seed='7')
