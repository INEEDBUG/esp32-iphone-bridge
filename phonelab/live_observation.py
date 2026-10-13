"""Read-only loopback client for already-running PhoneLab screen observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import re
import struct
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib


BASE_URL = "http://127.0.0.1:8766"
_TARGET = re.compile(r"^iphone-[0-9a-f]{12}$")
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_PNG_BYTES = 20 * 1024 * 1024
_SCREEN_FIELDS = {
    "state", "target", "sequence", "width", "height", "orientation", "source",
    "source_time", "received_monotonic", "locked", "fresh",
}
_RUN_FIELDS = {"run_id", "test_id", "seed", "nonce", "last_seq", "phone_verified"}
_EVENT_ROW_FIELDS = {"recorded_at", "recorded_monotonic", "source", "run_id", "test_id", "seed", "seq", "event"}


class ObservationError(RuntimeError):
    """Coarse read-only observation failure; never includes request/token details."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ScreenSnapshot:
    image_bytes: bytes = field(repr=False)
    metadata: dict = field(repr=False)
    age_seconds: float

    def __repr__(self) -> str:
        return (f"ScreenSnapshot(sequence={self.metadata['sequence']}, "
                f"width={self.metadata['width']}, height={self.metadata['height']}, "
                f"age_seconds={self.age_seconds:.3f})")


def _validate_origin(value: str) -> str:
    if value != BASE_URL:
        raise ValueError("observation endpoint must remain fixed to loopback")
    return value


def _private_root(value: Path) -> Path:
    root = Path(value)
    if not root.is_absolute():
        raise ValueError("an absolute private evidence root is required")
    resolved = root.resolve()
    project_root = Path(__file__).resolve().parents[1]
    if resolved == project_root or project_root in resolved.parents:
        raise ValueError("evidence root must stay outside the source repository")
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            raise ValueError("evidence root must stay outside Git repositories")
    return resolved


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if not isinstance(payload, bytes) or payload[:8] != _PNG_SIGNATURE or len(payload) > _MAX_PNG_BYTES:
        raise ObservationError("screen image is not an acceptable PNG")
    offset = 8
    width = height = None
    saw_idat = False
    saw_iend = False
    while offset + 12 <= len(payload):
        length = struct.unpack_from(">I", payload, offset)[0]
        end = offset + 12 + length
        if end > len(payload):
            break
        kind = payload[offset + 4:offset + 8]
        data = payload[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack_from(">I", payload, offset + 8 + length)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise ObservationError("screen PNG chunk checksum is invalid")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ObservationError("screen PNG header is invalid")
            width, height = struct.unpack_from(">II", data)
            if not width or not height:
                raise ObservationError("screen PNG dimensions are invalid")
        elif kind == b"IHDR":
            raise ObservationError("screen PNG has an invalid header sequence")
        if kind == b"IDAT":
            saw_idat = True
        if kind == b"IEND":
            if length != 0:
                raise ObservationError("screen PNG end chunk is invalid")
            saw_iend = True
            offset = end
            break
        offset = end
    if not saw_idat or not saw_iend or offset != len(payload) or width is None or height is None:
        raise ObservationError("screen PNG is incomplete")
    return width, height


def _validate_run(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _RUN_FIELDS:
        raise ObservationError("observation run has an invalid structure")
    if (not isinstance(value["run_id"], str) or not _RUN_ID.fullmatch(value["run_id"])
            or type(value["last_seq"]) is not int or value["last_seq"] < 0
            or value["phone_verified"] is not False):
        raise ObservationError("observation run failed validation")
    for name in ("test_id", "seed", "nonce"):
        item = value[name]
        if not isinstance(item, str) or not 1 <= len(item) <= 128 or not item.isascii():
            raise ObservationError("observation run failed validation")
    return dict(value)


class LiveObservationClient:
    """Fetch authenticated screen and event observations; exposes no control methods."""

    def __init__(self, base_url: str, token: str, target: str, *, opener=None, clock=None):
        if not isinstance(token, str) or not token:
            raise ValueError("control token is required")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise ValueError("expected target label is invalid")
        self._base_url = _validate_origin(base_url)
        self._control_token = token
        self.target = target
        self._opener = opener or build_opener(_NoRedirect()).open
        self._clock = clock or time.monotonic

    def __repr__(self) -> str:
        return f"LiveObservationClient(target={self.target!r}, token=<redacted>)"

    def _get(self, path: str, *, timeout: float = 3.0):
        request = Request(self._base_url + path, headers={
            "Origin": self._base_url,
            "X-PhoneLab-Control": self._control_token,
        }, method="GET")
        try:
            response = self._opener(request, timeout=timeout)
            with response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if status != 200:
                    raise ObservationError("observation endpoint returned an unavailable response")
                geturl = getattr(response, "geturl", None)
                if callable(geturl) and geturl() != request.full_url:
                    raise ObservationError("observation response left the fixed loopback endpoint")
                body = response.read(_MAX_PNG_BYTES + 1)
                headers = response.headers
            return body, headers
        except ObservationError:
            raise
        except Exception:
            raise ObservationError("observation request failed") from None

    def snapshot(self) -> ScreenSnapshot:
        payload, headers = self._get("/screen")
        if len(payload) > _MAX_PNG_BYTES:
            raise ObservationError("screen image exceeds the size limit")
        try:
            raw_metadata = headers.get("X-PhoneLab-Frame")
            metadata = json.loads(raw_metadata)
        except Exception:
            raise ObservationError("screen metadata is unavailable") from None
        if not isinstance(metadata, dict) or set(metadata) != _SCREEN_FIELDS:
            raise ObservationError("screen metadata has an invalid structure")
        receipt = metadata.get("received_monotonic")
        now = self._clock()
        if (metadata.get("state") != "fresh" or metadata.get("fresh") is not True
                or metadata.get("target") != self.target or metadata.get("orientation") != "portrait"
                or metadata.get("sequence") is None or type(metadata["sequence"]) is not int
                or not 1 <= metadata["sequence"] <= 21600
                or metadata.get("width") != 828 or metadata.get("height") != 1792
                or metadata.get("source") != "USB_SCREENSHOT" or metadata.get("source_time") is not None
                or metadata.get("locked") != "UNKNOWN"
                or type(receipt) not in (int, float) or not math.isfinite(receipt)
                or type(now) not in (int, float) or not math.isfinite(now)):
            raise ObservationError("screen metadata failed validation")
        age = now - receipt
        if not 0 <= age <= 2.0:
            raise ObservationError("screen frame is stale")
        if _png_dimensions(payload) != (828, 1792):
            raise ObservationError("screen PNG dimensions do not match metadata")

        return ScreenSnapshot(payload, dict(metadata), age)

    def save_snapshot(self, snapshot: ScreenSnapshot, output_root: Path, name: str) -> Path:
        if (not isinstance(snapshot, ScreenSnapshot) or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name)):
            raise ValueError("snapshot output name is invalid")
        root = _private_root(output_root)
        directory = root / name
        try:
            directory.mkdir(parents=True, exist_ok=False)
            (directory / "screen.png").write_bytes(snapshot.image_bytes)
            record = {"source": "PhoneLab /screen", "captured_at": datetime.now(timezone.utc).isoformat(),
                      "age_seconds": snapshot.age_seconds, "metadata": snapshot.metadata}
            (directory / "metadata.json").write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
        except Exception:
            raise ObservationError("private screen evidence could not be saved") from None
        return directory

    def runs(self) -> list[dict]:
        body, _ = self._get("/observations")
        try:
            result = json.loads(body)
        except Exception:
            raise ObservationError("observation index is invalid") from None
        if not isinstance(result, dict) or set(result) != {"source", "runs"} or result["source"] != "synthetic_page_observation" or not isinstance(result["runs"], list):
            raise ObservationError("observation index has an invalid structure")
        runs = [_validate_run(item) for item in result["runs"]]
        if len({run["run_id"] for run in runs}) != len(runs):
            raise ObservationError("observation index contains duplicate runs")
        return runs

    def events(self, run_id: str) -> dict:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("observation run id is invalid")
        body, _ = self._get("/observations?run_id=" + quote(run_id, safe=""))
        try:
            result = json.loads(body)
        except Exception:
            raise ObservationError("observation run is invalid") from None
        if (not isinstance(result, dict) or set(result) != {"source", "run", "events"}
                or result["source"] != "synthetic_page_observation"):
            raise ObservationError("observation run has an invalid structure")
        run = _validate_run(result["run"])
        if run["run_id"] != run_id or not isinstance(result["events"], list):
            raise ObservationError("observation run does not match the request")
        previous = 0
        events = []
        for row in result["events"]:
            if (not isinstance(row, dict) or set(row) != _EVENT_ROW_FIELDS
                    or row.get("source") != "client_reported_browser_event"
                    or row.get("run_id") != run_id or row.get("test_id") != run["test_id"]
                    or row.get("seed") != run["seed"] or type(row.get("seq")) is not int
                    or row["seq"] <= previous or not isinstance(row.get("event"), dict)
                    or type(row.get("recorded_monotonic")) not in (int, float)
                    or not math.isfinite(row["recorded_monotonic"])
                    or not isinstance(row.get("recorded_at"), str)):
                raise ObservationError("browser event row failed validation")
            previous = row["seq"]
            events.append(dict(row))
        if previous > run["last_seq"]:
            raise ObservationError("browser event sequence exceeds the run summary")
        clean = {"source": result["source"], "run": run, "events": events}
        return {"run": run, "events": events}

    def visible_run(self, *, test_id: str, seed: str) -> dict:
        """Associate a server-issued run with one real atomic marker observation.

        Refreshing creates a new nonce. Never guess by list order or test ID.
        This single-frame association grants neither input nor lock-state proof.
        """
        from .visual_marker import decode_visual_marker, MarkerError
        snapshot = self.snapshot()
        candidates = [run for run in self.runs()
                      if run['test_id'] == test_id and run['seed'] == seed]
        matches = []
        for run in candidates:
            try:
                decode_visual_marker(snapshot.image_bytes, run['nonce'])
            except MarkerError:
                continue
            matches.append(run)
        if len(matches) != 1:
            raise ObservationError('visible page run is unavailable or ambiguous')
        if not 0 <= self._clock() - snapshot.metadata['received_monotonic'] <= 2:
            raise ObservationError('visible page association became stale')
        return matches[0]
