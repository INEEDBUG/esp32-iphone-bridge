"""Loopback-only browser test surface and guarded controller HTTP API."""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


MAX_BODY_BYTES = 64 * 1024
MAX_EVENT_ROWS = 20_000
MAX_RUN_SESSIONS = 64
SYNTHETIC_TEXT_CORPUS = frozenset({
    "test123",
    "TEST123",
    "Test 123",
    "test123!@#",
    'ASCII symbols: !"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~',
})
_EVENT_COMMON_FIELDS = frozenset({
    "type", "isTrusted", "target", "timeStamp", "clientX", "clientY", "button",
})
_EVENT_FIELDS = {
    **{name: _EVENT_COMMON_FIELDS for name in (
        "pointerdown", "pointerup", "pointermove", "click", "keydown", "dragstart", "dragover",
    )},
    "beforeinput": _EVENT_COMMON_FIELDS | {"textLength", "syntheticTextMatch"},
    "input": _EVENT_COMMON_FIELDS | {"textLength", "syntheticTextMatch"},
    "drop": _EVENT_COMMON_FIELDS | {"dragState"},
    "drag_result": frozenset({"type", "isTrusted", "completed", "dropCount",
                              "trustedPointerSequence", "dragState"}),
    "field_state": frozenset({"type", "isTrusted", "text", "textLength", "syntheticTextPrefix",
                               "focused", "caretAtEnd", "fromTrustedInput"}),
    "scroll": _EVENT_COMMON_FIELDS | {"scrollX", "scrollY"},
    "page_geometry": _EVENT_COMMON_FIELDS | {
        "innerWidth", "innerHeight", "visualWidth", "visualHeight",
        "visualOffsetLeft", "visualOffsetTop", "devicePixelRatio",
    },
    "final_state": frozenset({
        "type", "isTrusted", "text", "textLength", "syntheticTextMatch",
        "scrollX", "scrollY", "dragState",
    }),
}
_EVENT_TARGETS = frozenset({
    "window", "document", "element", "html", "body", "header", "h1", "div", "b", "p",
    "section", "h2", "button", "input", "label", "span", "pre", "test-text", "drag-source",
    "drop-target", "drop-state", "save-final", "probe-grid", "probe-summary", "test-id", "seed", "nonce", "delivery",
    "log", "scroll-marker-one", "scroll-marker-two", "scroll-marker-three", "native-scroll",
    *(f"{number:02d}" for number in range(1, 26)),
})
SAFE_CONTROL_OPS = {
    "move", "click", "press", "release", "scroll", "type", "key", "ping", "home"
}
STATIC_DIR = Path(__file__).with_name("static")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _valid_control_request(body: dict) -> bool:
    if set(body) != {"id", "session", "owner", "deadline", "op", "args"}:
        return False
    for name in ("id", "session", "owner"):
        value = body.get(name)
        if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isascii():
            return False
    deadline = body.get("deadline")
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return False
    if not time.monotonic() <= deadline <= time.monotonic() + 5:
        return False
    operation = body.get("op")
    args = body.get("args")
    if not isinstance(operation, str) or operation not in SAFE_CONTROL_OPS or not isinstance(args, dict):
        return False
    if operation == "move":
        return (
            set(args) == {"dx", "dy"}
            and all(isinstance(args[k], int) and not isinstance(args[k], bool) and -2048 <= args[k] <= 2048 for k in ("dx", "dy"))
        )
    if operation == "scroll":
        return (
            set(args) == {"v", "h"}
            and all(isinstance(args[k], int) and not isinstance(args[k], bool) and -127 <= args[k] <= 127 for k in ("v", "h"))
        )
    if operation == "type":
        text = args.get("text")
        return (
            set(args) == {"text"}
            and isinstance(text, str)
            and len(text) <= 128
            and text.isascii()
            and all(32 <= ord(char) <= 126 for char in text)
        )
    if operation == "key":
        return set(args) == {"key"} and args.get("key") in {"enter", "backspace", "escape", "tab"}
    return not args


def _finite_number(value: object, low: float, high: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and low <= value <= high
        and math.isfinite(value)
    )


def _valid_browser_event(event: dict) -> bool:
    event_type = event.get("type")
    fields = _EVENT_FIELDS.get(event_type) if isinstance(event_type, str) else None
    if fields is None or set(event) != fields or type(event.get("isTrusted")) is not bool:
        return False
    if event_type == "field_state":
        text=event.get('text');length=event.get('textLength');prefix=event.get('syntheticTextPrefix')
        if (event['isTrusted'] is not False or type(length) is not int or not 0<=length<=2000
                or any(type(event.get(name)) is not bool for name in ('syntheticTextPrefix','focused','caretAtEnd','fromTrustedInput'))):
            return False
        return ((text is None and prefix is False) or (isinstance(text,str) and prefix is True
                and length==len(text) and any(value.startswith(text) for value in SYNTHETIC_TEXT_CORPUS)))
    if event_type == "drag_result":
        return (event['isTrusted'] is False and type(event.get('completed')) is bool
                and type(event.get('dropCount')) is int and 0 <= event['dropCount'] <= 999999
                and event.get('trustedPointerSequence') is True
                and event.get('dragState') == ('dropped' if event['completed'] else 'released-outside'))
    if event_type != "final_state":
        target = event.get("target")
        if not isinstance(target, str) or target not in _EVENT_TARGETS:
            return False
        if type(event.get("timeStamp")) not in (int, float) or not _finite_number(event["timeStamp"], 0, 1_000_000_000_000):
            return False
        if any(event.get(name) is not None and not _finite_number(event[name], -1_000_000, 1_000_000)
               for name in ("clientX", "clientY")):
            return False
        button = event.get("button")
        if button is not None and (type(button) is not int or not -1 <= button <= 2):
            return False
    if event_type in ("input", "beforeinput", "final_state"):
        length = event.get("textLength")
        if type(length) is not int or not 0 <= length <= 2000:
            return False
        matched = event.get("syntheticTextMatch")
        if type(matched) is not bool:
            return False
        if event_type == "final_state":
            text = event.get("text")
            if text is None:
                if matched:
                    return False
            elif not isinstance(text, str) or text not in SYNTHETIC_TEXT_CORPUS or not matched or len(text) != length:
                return False
        elif matched and length not in {len(text) for text in SYNTHETIC_TEXT_CORPUS}:
            return False
    if event_type == "drop":
        drag_state = event.get("dragState")
        if not isinstance(drag_state, str) or drag_state not in {"started", "dropped"}:
            return False
    if event_type == "scroll":
        if not all(_finite_number(event.get(name), -1_000_000, 1_000_000) for name in ("scrollX", "scrollY")):
            return False
    if event_type == "page_geometry":
        if (event["isTrusted"] is not False or event["target"] != "window"
                or any(event.get(name) is not None for name in ("clientX", "clientY", "button"))):
            return False
        if any(type(event.get(name)) is not int or not 1 <= event[name] <= 100_000
               for name in ("innerWidth", "innerHeight")):
            return False
        if not all(_finite_number(event.get(name), 1, 100_000)
                   for name in ("visualWidth", "visualHeight")):
            return False
        if not all(_finite_number(event.get(name), -100_000, 100_000)
                   for name in ("visualOffsetLeft", "visualOffsetTop")):
            return False
        if not _finite_number(event.get("devicePixelRatio"), 0.1, 16):
            return False
    if event_type == "final_state":
        if not all(_finite_number(event.get(name), -1_000_000, 1_000_000) for name in ("scrollX", "scrollY")):
            return False
        drag_state = event.get("dragState")
        if not isinstance(drag_state, str) or drag_state not in {"not-started", "started", "pressed", "moving", "dropped", "released-outside", "cancelled"}:
            return False
    return True


def create_server(host: str = "127.0.0.1", port: int = 0, data_root: Path = Path("data"), controller=None,
                  screen_runtime=None):
    """Create a loopback-only server. LAN binds are intentionally refused."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("PhoneLab web server may bind to loopback only")
    server = ThreadingHTTPServer((host, port), _handler_for(Path(data_root), controller))
    server.daemon_threads = True
    server.control_token = secrets.token_urlsafe(32)
    server.data_root = Path(data_root)
    server.controller = controller
    server.screen_runtime = screen_runtime
    from .stop_signal import StopStore
    server.stop_store = StopStore(server.data_root / 'private')
    return server


def run_server(host: str = "127.0.0.1", port: int = 0, data_root: Path = Path("data"), controller=None,
               screen_runtime=None):
    """Create and run the loopback server until interrupted."""
    server = create_server(host=host, port=port, data_root=data_root, controller=controller,
                           screen_runtime=screen_runtime)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _handler_for(data_root: Path, controller):
    state_lock = threading.Lock()
    sessions: dict[str, dict] = {}
    event_count = 0
    event_path = data_root / "test-events" / "events.jsonl"

    def observed_events(run_id):
        rows = []
        with state_lock:
            if event_path.exists():
                with event_path.open('r', encoding='utf-8') as stream:
                    for line in stream:
                        if line.strip():
                            row = json.loads(line)
                            if isinstance(row, dict) and row.get('run_id') == run_id:
                                rows.append(row)
        return rows

    class Handler(BaseHTTPRequestHandler):
        server_version = "PhoneLab/0.1"
        sys_version = ""

        def log_message(self, format, *args):
            # Avoid logging tokens, query strings, event text, or request bodies.
            return

        def _send(self, status: int, payload: bytes, content_type: str, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, value: object):
            self._send(status, _json_bytes(value), "application/json; charset=utf-8")

        def _screen_status(self, value=None):
            runtime = getattr(self.server, "screen_runtime", None)
            if runtime is None:
                return {"state": "UNKNOWN", "target": None, "sequence": None,
                        "width": None, "height": None, "orientation": None,
                        "source": None, "source_time": None, "received_monotonic": None,
                        "locked": "UNKNOWN", "fresh": False}
            try:
                value = runtime.status() if value is None else value
            except Exception:
                value = {}
            if not isinstance(value, dict):
                value = {}
            target = value.get("target")
            if not isinstance(target, str) or not re.fullmatch(r"iphone-[0-9a-f]{12}", target):
                target = None
            geometry = {}
            for name in ("sequence", "width", "height"):
                item = value.get(name)
                low = 1
                high = 21600 if name == 'sequence' else 10000
                geometry[name] = item if type(item) is int and low <= item <= high else None
            orientation = value.get("orientation")
            if orientation not in {"portrait", "landscape"}:
                orientation = None
            source = "USB_SCREENSHOT" if value.get("source") in {"USB_SCREENSHOT", "Apple DVT screenshot over existing USB pairing and in-process RSD"} else None
            received = value.get("received_monotonic")
            if not isinstance(received, (int, float)) or isinstance(received, bool) or not math.isfinite(received):
                received = None
            fresh = (value.get("fresh") is True and value.get("state") == "fresh" and target is not None
                     and geometry["sequence"] is not None and geometry["width"] is not None
                     and geometry["height"] is not None and orientation is not None
                     and source is not None and received is not None
                     and 0 <= time.monotonic() - received <= 2.0)
            return {"state": "fresh" if fresh else str(value.get("state", "UNKNOWN"))[:16],
                    "target": target, **geometry, "orientation": orientation, "source": source,
                    "source_time": None, "received_monotonic": received,
                    "locked": "UNKNOWN", "fresh": fresh}

        def _peer_is_local(self) -> bool:
            return bool(self.client_address) and _is_loopback(self.client_address[0])

        def _origin_is_local(self) -> bool:
            origin = self.headers.get("Origin", "")
            if not origin:
                return False
            parsed = urlsplit(origin)
            try:
                origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                host_port = self.headers.get("Host", "").rsplit(":", 1)
                if len(host_port) == 2:
                    request_port = int(host_port[1])
                    request_host = host_port[0].strip("[]")
                else:
                    request_port = 80
                    request_host = self.headers.get("Host", "").strip("[]")
            except (ValueError, TypeError):
                return False
            return (
                parsed.scheme == "http"
                and parsed.path == ""
                and parsed.query == ""
                and parsed.fragment == ""
                and (parsed.hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
                and _is_loopback(parsed.hostname or "")
                and request_host.lower() in {"127.0.0.1", "localhost", "::1"}
                and origin_port == request_port == self.server.server_port
            )

        def _read_json(self):
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "")
            except ValueError:
                self._json(400, {"error": "invalid_content_length"})
                return None
            if length < 0:
                self._json(400, {"error": "invalid_content_length"})
                return None
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "body_too_large", "max_bytes": MAX_BODY_BYTES})
                return None
            if self.headers.get_content_type() != "application/json":
                self._json(415, {"error": "application_json_required"})
                return None
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_json"})
                return None
            if not isinstance(value, dict):
                self._json(400, {"error": "json_object_required"})
                return None
            return value

        def do_GET(self):
            nonlocal event_count
            parsed = urlsplit(self.path)
            if not self._peer_is_local():
                self._json(403, {"error": "loopback_only"})
                return
            if parsed.path == "/health":
                self._json(200, {"status": "offline_not_real_phone"})
                return
            if parsed.path == "/status":
                if controller is None or not callable(getattr(controller, "get_status", None)):
                    self._json(200, {"state": "UNKNOWN"})
                    return
                try:
                    result = controller.get_status()
                except Exception:
                    self._json(503, {"state": "UNKNOWN", "error": "controller_status_unavailable"})
                    return
                capture = self._screen_status()
                if getattr(self.server, "screen_runtime", None) is not None:
                    allowed = {"state", "armed", "reason", "ACTIVE_INPUT_BACKEND", "INPUT_AVAILABLE",
                               "FRESH_SCREEN_AVAILABLE", "calibrated"}
                    safe = {key: value for key, value in result.items() if key in allowed} if isinstance(result, dict) else {"state": "UNKNOWN"}
                else:
                    safe = dict(result) if isinstance(result, dict) else {"state": "UNKNOWN"}
                safe["screen_capture"] = capture
                safe["SCREEN_CAPTURE_FRESH"] = "VERIFIED" if capture["fresh"] else "UNKNOWN"
                if getattr(self.server, "screen_runtime", None) is not None:
                    safe["FRESH_SCREEN_AVAILABLE"] = "VERIFIED" if capture["fresh"] else "UNKNOWN"
                self._json(200, safe)
                return
            if parsed.path == '/observations':
                if not self._origin_is_local():
                    self._json(403, {'error': 'same_origin_required'})
                    return
                supplied = self.headers.get('X-PhoneLab-Control', '')
                if not supplied or not hmac.compare_digest(supplied, self.server.control_token):
                    self._json(403, {'error': 'control_token_required'})
                    return
                query = parse_qs(parsed.query, keep_blank_values=True)
                if query and (set(query) != {'run_id'} or len(query['run_id']) != 1
                              or not re.fullmatch(r'[0-9a-f]{16}', query['run_id'][0])):
                    self._json(400, {'error': 'invalid_observation_query'})
                    return
                with state_lock:
                    runs = [{key: value for key, value in session.items()
                             if key in {'run_id', 'test_id', 'seed', 'nonce', 'last_seq'}}
                            for session in sessions.values()]
                for run in runs:
                    run['phone_verified'] = False
                if not query:
                    self._json(200, {'source': 'synthetic_page_observation', 'runs': runs})
                    return
                run_id = query['run_id'][0]
                run = next((r for r in runs if r['run_id'] == run_id), None)
                if run is None:
                    self._json(404, {'error': 'observation_run_unavailable'})
                    return
                try:
                    rows = observed_events(run_id)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    self._json(500, {'error': 'event_store_unavailable'})
                    return
                self._json(200, {'source': 'synthetic_page_observation', 'run': run, 'events': rows})
                return
            if parsed.path == "/screen":
                runtime = getattr(self.server, "screen_runtime", None)
                if runtime is None:
                    self._json(503, {"error": "screen_capture_unavailable"})
                    return
                if not self._origin_is_local():
                    self._json(403, {"error": "same_origin_required"})
                    return
                supplied = self.headers.get("X-PhoneLab-Control", "")
                if not supplied or not hmac.compare_digest(supplied, self.server.control_token):
                    self._json(403, {"error": "control_token_required"})
                    return
                try:
                    snapshot = runtime.snapshot()
                    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('metadata'), dict):
                        raise ValueError('no current snapshot')
                    metadata = self._screen_status(snapshot['metadata'])
                    payload = snapshot['image_bytes']
                except Exception:
                    metadata, payload = {}, None
                if (not isinstance(payload, bytes) or not payload.startswith(b"\x89PNG\r\n\x1a\n")
                        or len(payload) > 20 * 1024 * 1024 or not metadata.get("fresh")
                        or metadata.get("target") is None):
                    self._json(503, {"error": "fresh_screen_unavailable"})
                    return
                self._send(200, payload, "image/png", {
                    'X-PhoneLab-Frame': json.dumps(metadata, ensure_ascii=True, separators=(',', ':'))})
                return
            if parsed.path == "/events":
                token = self.headers.get("X-PhoneLab-Run", "")
                if not token:
                    self._json(403, {"error": "run_token_required"})
                    return
                with state_lock:
                    session = sessions.get(token)
                if session is None:
                    self._json(403, {"error": "invalid_run_session"})
                    return
                run_id = session["run_id"]
                try:
                    rows = observed_events(run_id)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    self._json(500, {"error": "event_store_unavailable"})
                    return
                self._json(200, {"source": "browser_event_observation", "events": rows})
                return
            if parsed.path == "/lab":
                query = parse_qs(parsed.query, keep_blank_values=True)
                test_id = query.get("test_id", ["browser-lab"])[0]
                seed = query.get("seed", ["1"])[0]
                if (not test_id or not test_id.isascii() or len(test_id) > 64 or not all(c.isalnum() or c in "_.-" for c in test_id)
                        or not seed or not seed.isascii() or len(seed) > 64 or not all(c.isalnum() or c in "_.-" for c in seed)):
                    self._json(400, {"error": "invalid_test_id_or_seed"})
                    return
                token = secrets.token_urlsafe(32)
                nonce = secrets.token_urlsafe(12)
                with state_lock:
                    if len(sessions) >= MAX_RUN_SESSIONS:
                        self._json(429, {'error': 'run_session_limit_reached'})
                        return
                    sessions[token] = {
                        "test_id": test_id, "seed": seed, "last_seq": 0,
                        "run_id": secrets.token_hex(8), 'nonce': nonce,
                    }
                try:
                    template = (STATIC_DIR / "lab.html").read_text(encoding="utf-8")
                except OSError:
                    self._json(500, {"error": "lab_page_unavailable"})
                    return
                config = _json_bytes({"testId": test_id, "seed": seed, "runToken": token, "nonce": nonce})
                safe_config = config.decode("utf-8").replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                page = template.replace("__PHONE_LAB_CONFIG__", safe_config).encode("utf-8")
                self._send(200, page, "text/html; charset=utf-8")
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self):
            nonlocal event_count
            parsed = urlsplit(self.path)
            if not self._peer_is_local():
                self._json(403, {"error": "loopback_only"})
                return
            body = self._read_json()
            if body is None:
                return
            if parsed.path == "/events":
                token = self.headers.get("X-PhoneLab-Run", "")
                if not token:
                    self._json(403, {"error": "run_token_required"})
                    return
                test_id, seq, event = body.get("test_id"), body.get("seq"), body.get("event")
                if (set(body) != {"test_id", "seq", "event"} or not isinstance(test_id, str)
                        or not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(event, dict)
                        or not _valid_browser_event(event)):
                    self._json(400, {"error": "invalid_event_envelope"})
                    return
                with state_lock:
                    session = sessions.get(token)
                    if session is None or not hmac.compare_digest(session["test_id"], test_id):
                        self._json(403, {"error": "invalid_run_session"})
                        return
                    if seq <= session["last_seq"]:
                        self._json(409, {"error": "nonmonotonic_sequence"})
                        return
                    if event_count >= MAX_EVENT_ROWS:
                        self._json(429, {"error": "event_limit_reached"})
                        return
                    session["last_seq"] = seq
                    event_count += 1
                    row = {
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        'recorded_monotonic': time.monotonic(),
                        "source": "client_reported_browser_event",
                        "run_id": session["run_id"],
                        "test_id": test_id,
                        "seed": session["seed"],
                        "seq": seq,
                        "event": event,
                    }
                    event_path.parent.mkdir(parents=True, exist_ok=True)
                    with event_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                self._json(201, {"stored": True, "source": "client_reported_browser_event"})
                return
            if parsed.path in {"/control", "/stop"}:
                if not self._origin_is_local():
                    self._json(403, {"error": "same_origin_required"})
                    return
                supplied = self.headers.get("X-PhoneLab-Control", "")
                if not supplied or not hmac.compare_digest(supplied, self.server.control_token):
                    self._json(403, {"error": "control_token_required"})
                    return
                if parsed.path == '/stop':
                    published = local_stopped = False
                    try:
                        self.server.stop_store.publish_stop()
                        published = True
                    except Exception:
                        pass
                    # Always attempt local cleanup, including a store failure.
                    # Publishing never opens a serial port or unknown firmware.
                    try:
                        if controller is not None:
                            controller.stop()
                            local_stopped = True
                    except Exception:
                        pass
                    ok = published and (local_stopped or controller is None)
                    self._json(200 if ok else 503, {
                        'stopped': ok,
                        'shared_stop': 'published' if published else 'failed',
                        'local_controller_stopped': local_stopped,
                        'release_confirmed': False})
                    return
                if controller is None:
                    self._json(503, {"error": "controller_unavailable"})
                    return
                try:
                    if not _valid_control_request(body):
                        self._json(400, {"error": "unsupported_operation"})
                        return
                    result = controller.submit(body)
                except Exception as exc:
                    if exc.__class__.__name__ == "LabError":
                        self._json(400, {"error": "controller_rejected_request"})
                        return
                    if isinstance(exc, (ValueError, TypeError)):
                        self._json(400, {"error": "invalid_controller_request"})
                        return
                    self._json(503, {"error": "controller_request_failed"})
                    return
                self._json(200, result if isinstance(result, dict) else {"accepted": bool(result)})
                return
            self._json(404, {"error": "not_found"})

    return Handler
