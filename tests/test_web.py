import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from phonelab.web import create_server
from phonelab.control import Controller as CoreController


class Controller:
    def __init__(self):
        self.submitted = []
        self.stopped = False

    def get_status(self):
        return {"state": "connected", "identity": "test-double", "session": "secret-session",
                "owner": "operator", "armed": False, "ACTIVE_INPUT_BACKEND": "none",
                "INPUT_AVAILABLE": "UNKNOWN", "FRESH_SCREEN_AVAILABLE": "UNKNOWN"}

    def submit(self, request):
        self.submitted.append(request)
        return {"accepted": True}

    def stop(self):
        self.stopped = True
        return {"stopped": True}


class FakeScreenRuntime:
    def __init__(self):
        self.fresh = True
        self.png = b"\x89PNG\r\n\x1a\nprivate"
        self.received = time.monotonic()
        self.sequence = 3

    def status(self):
        return {"state": "fresh" if self.fresh else "stale", "target": "iphone-ac39233f6b70",
                "sequence": self.sequence, "width": 828, "height": 1792, "orientation": "portrait",
                "source": "USB_SCREENSHOT", "source_time": None,
                "received_monotonic": self.received,
                "locked": "UNKNOWN", "fresh": self.fresh}

    def screen(self):
        return self.png if self.fresh else None

    def snapshot(self):
        return {'image_bytes': self.png, 'metadata': self.status()} if self.fresh else None


class WebSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = Controller()
        self.server = create_server(
            host="127.0.0.1", port=0, data_root=Path(self.temp.name), controller=self.controller
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def get(self, path, headers=None):
        request = Request(self.base + path, headers=headers or {}, method="GET")
        with urlopen(request, timeout=2) as response:
            return response.status, response.headers, response.read()

    def test_observation_index_is_authenticated_and_never_exposes_browser_run_token(self):
        _, _, raw = self.get('/lab?test_id=observer-unit&seed=42')
        config = json.loads(re.search(rb'const config = (\{[^\n]+\});', raw).group(1))
        headers = {'Origin': self.base, 'X-PhoneLab-Control': self.server.control_token}
        for supplied in ({}, {'X-PhoneLab-Control': self.server.control_token},
                         {'Origin': self.base, 'X-PhoneLab-Control': config['runToken']}):
            with self.assertRaises(HTTPError) as caught:
                self.get('/observations', supplied)
            self.assertEqual(caught.exception.code, 403)
        _, _, response = self.get('/observations', headers)
        index = json.loads(response)
        self.assertEqual(index['source'], 'synthetic_page_observation')
        self.assertEqual(len(index['runs']), 1)
        run = index['runs'][0]
        self.assertEqual(run['nonce'], config['nonce'])
        self.assertNotIn(config['runToken'], response.decode())
        self.assertNotIn(self.server.control_token, response.decode())
        self.assertFalse(run['phone_verified'])
        _, _, response = self.get('/observations?run_id=' + run['run_id'], headers)
        self.assertEqual(json.loads(response)['events'], [])
        event = {'type': 'click', 'isTrusted': True, 'target': '01', 'timeStamp': 1,
                 'clientX': 10, 'clientY': 20, 'button': 0}
        self.post('/events', {'test_id': config['testId'], 'seq': 1, 'event': event},
                  {'X-PhoneLab-Run': config['runToken']})
        _, _, response = self.get('/observations?run_id=' + run['run_id'], headers)
        observed = json.loads(response)
        self.assertEqual(observed['run']['last_seq'], 1)
        self.assertEqual(observed['events'][0]['event'], event)
        self.assertIsInstance(observed['events'][0]['recorded_monotonic'], float)
        self.assertEqual(self.controller.submitted, [])
        self.assertNotIn(config['runToken'], response.decode())
        with self.assertRaises(HTTPError) as caught:
            self.get('/observations?run_id=' + run['run_id'] + '&run_id=' + run['run_id'], headers)
        self.assertEqual(caught.exception.code, 400)

    def test_observation_registry_limit_preserves_existing_run(self):
        from unittest.mock import patch
        headers = {'Origin': self.base, 'X-PhoneLab-Control': self.server.control_token}
        with patch('phonelab.web.MAX_RUN_SESSIONS', 2, create=True):
            self.get('/lab?test_id=first&seed=1')
            self.get('/lab?test_id=second&seed=1')
            with self.assertRaises(HTTPError) as caught:
                self.get('/lab?test_id=third&seed=1')
            self.assertEqual(caught.exception.code, 429)
            _, _, raw = self.get('/observations', headers)
            self.assertEqual([r['test_id'] for r in json.loads(raw)['runs']], ['first', 'second'])

    def post(self, path, body, headers=None):
        request = Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        return urlopen(request, timeout=2)

    def test_health_and_status_do_not_claim_phone_presence(self):
        _, _, health = self.get("/health")
        self.assertEqual(json.loads(health), {"status": "offline_not_real_phone"})
        _, _, status = self.get("/status")
        self.assertEqual(json.loads(status)["identity"], "test-double")

    def test_long_capture_sequence_is_supported_without_weakening_geometry_bounds(self):
        screen = FakeScreenRuntime(); screen.sequence = 10001
        self.server.screen_runtime = screen
        headers = {'Origin': self.base, 'X-PhoneLab-Control': self.server.control_token}
        code, response_headers, body = self.get('/screen', headers)
        self.assertEqual(code, 200)
        self.assertEqual(body, screen.png)
        self.assertEqual(json.loads(response_headers['X-PhoneLab-Frame'])['sequence'], 10001)

    def test_screen_bytes_and_metadata_come_from_one_atomic_snapshot(self):
        class AtomicRuntime(FakeScreenRuntime):
            def status(self): raise AssertionError('separate metadata read')
            def screen(self): raise AssertionError('separate pixel read')
            def snapshot(self): return {'image_bytes': self.png, 'metadata': FakeScreenRuntime.status(self)}
        screen = AtomicRuntime(); self.server.screen_runtime = screen
        code, headers, body = self.get('/screen', {
            'Origin': self.base, 'X-PhoneLab-Control': self.server.control_token})
        self.assertEqual(code, 200)
        self.assertEqual(body, screen.png)
        metadata = json.loads(headers['X-PhoneLab-Frame'])
        self.assertEqual(metadata['sequence'], screen.sequence)
        self.assertEqual(metadata['received_monotonic'], screen.received)
        self.assertEqual(metadata['source_time'], None)

    def test_screen_requires_control_origin_and_returns_only_fresh_png(self):
        screen = FakeScreenRuntime()
        other = create_server(host="127.0.0.1", port=0, data_root=Path(self.temp.name),
                              controller=self.controller, screen_runtime=screen)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{other.server_port}"
        try:
            unauthenticated = Request(base + "/screen", method="GET")
            with self.assertRaises(HTTPError) as denied:
                urlopen(unauthenticated, timeout=2)
            self.assertEqual(denied.exception.code, 403)
            with self.assertRaises(HTTPError) as missing_origin:
                urlopen(Request(base + "/screen", headers={"X-PhoneLab-Control": other.control_token}), timeout=2)
            self.assertEqual(missing_origin.exception.code, 403)
            with self.assertRaises(HTTPError) as foreign_origin:
                urlopen(Request(base + "/screen", headers={"Origin": "http://evil.example",
                                                               "X-PhoneLab-Control": other.control_token}), timeout=2)
            self.assertEqual(foreign_origin.exception.code, 403)
            headers = {"Origin": base, "X-PhoneLab-Control": other.control_token}
            request = Request(base + "/screen", headers=headers, method="GET")
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertEqual(response.read(), screen.png)
            with urlopen(Request(base + "/status")) as response:
                status = json.loads(response.read())
            self.assertEqual(status["screen_capture"]["locked"], "UNKNOWN")
            self.assertEqual(status["screen_capture"]["target"], "iphone-ac39233f6b70")
            self.assertNotIn("session", status)
            self.assertNotIn("owner", status)
            self.assertEqual(status["INPUT_AVAILABLE"], "UNKNOWN")
            self.assertEqual(status["FRESH_SCREEN_AVAILABLE"], "VERIFIED")
            self.assertEqual(status["SCREEN_CAPTURE_FRESH"], "VERIFIED")
            self.assertFalse(status["armed"])
            screen.received = time.monotonic() - 3.0
            with self.assertRaises(HTTPError) as too_old:
                urlopen(Request(base + "/screen", headers=headers), timeout=2)
            self.assertEqual(too_old.exception.code, 503)
            screen.fresh = False
            with self.assertRaises(HTTPError) as stale:
                urlopen(Request(base + "/screen", headers=headers), timeout=2)
            self.assertEqual(stale.exception.code, 503)
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=2)

    def test_missing_controller_status_is_explicitly_unknown(self):
        other = create_server(host="127.0.0.1", port=0, data_root=Path(self.temp.name))
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{other.server_port}/status") as response:
                self.assertEqual(json.loads(response.read()), {"state": "UNKNOWN"})
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=2)

    def test_core_controller_without_backend_fails_closed(self):
        core = CoreController(target="explicit-target")
        session = core.acquire("owner_1")
        other = create_server(host="127.0.0.1", port=0, data_root=Path(self.temp.name), controller=core)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{other.server_port}"
        try:
            with urlopen(base + "/status") as response:
                status = json.loads(response.read())
            self.assertEqual(status["INPUT_AVAILABLE"], "UNKNOWN")
            self.assertEqual(status["ACTIVE_INPUT_BACKEND"], "none")
            body = {
                "id": "request-1", "session": session, "owner": "owner_1",
                "deadline": time.monotonic() + 2, "op": "click", "args": {}
            }
            request = Request(base + "/control", data=json.dumps(body).encode(), method="POST", headers={
                "Content-Type": "application/json", "Origin": base,
                "X-PhoneLab-Control": other.control_token
            })
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, 400)
            self.assertFalse(core.get_status()["armed"])
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=2)

    def test_lab_issues_private_token_and_exposes_real_event_probes(self):
        _, _, page = self.get("/lab?test_id=caseA&seed=19")
        _, _, second_page = self.get("/lab?test_id=caseA&seed=19")
        html = page.decode()
        second_html = second_page.decode()
        self.assertIn("caseA", html)
        self.assertIn("isTrusted", html)
        self.assertIn("beforeinput", html)
        self.assertIn("clientX:", html)
        self.assertIn("clientY:", html)
        self.assertIn("button:", html)
        self.assertIn('"keydown"', html)
        self.assertIn('"input"', html)
        self.assertIn('"drop"', html)
        self.assertIn("dragState", html)
        self.assertIn("dragstart", html)
        self.assertIn("scroll", html)
        self.assertIn("nonce", html)
        self.assertGreaterEqual(html.count('class="probe"'), 25)
        config = json.loads(html.split("const config = ", 1)[1].split(";", 1)[0])
        second_config = json.loads(second_html.split("const config = ", 1)[1].split(";", 1)[0])
        self.assertNotEqual(config["nonce"], second_config["nonce"])
        self.assertNotIn("dispatchEvent", html)
        self.assertNotIn(self.server.control_token, html)

    def test_lab_client_emits_only_synthetic_text_and_no_raw_key_input(self):
        _, _, page = self.get("/lab?test_id=privacy&seed=4")
        html = page.decode()
        self.assertIn("isTrusted: event.isTrusted", html)
        self.assertIn('"test123"', html)
        self.assertIn('"TEST123"', html)
        self.assertIn('"Test 123"', html)
        self.assertIn('"test123!@#"', html)
        self.assertIn(r"ASCII symbols: !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~", html)
        self.assertNotIn("event.key", html)
        self.assertNotIn("event.data", html)
        self.assertNotIn("input.value.slice", html)
        self.assertIn("seq: entry.seq, event })", html)
        self.assertNotIn("seq: entry.seq, event: entry", html)

    def test_event_ingestion_checks_session_sequence_and_persists_jsonl(self):
        _, _, page = self.get("/lab?test_id=caseA&seed=19")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        event = {"type": "click", "isTrusted": True, "target": "01", "timeStamp": 12,
                 "clientX": 8, "clientY": 9, "button": 0}
        with self.post("/events", {"test_id": "caseA", "seq": 1, "event": event},
                       {"X-PhoneLab-Run": token}) as response:
            self.assertEqual(response.status, 201)
        for bad in (
            {"test_id": "other", "seq": 2, "event": event},
            {"test_id": "caseA", "seq": 1, "event": event},
        ):
            with self.assertRaises(HTTPError) as error:
                self.post("/events", bad, {"X-PhoneLab-Run": token})
            self.assertIn(error.exception.code, (400, 403, 409))
        with self.assertRaises(HTTPError) as error:
            self.get("/events")
        self.assertEqual(error.exception.code, 403)
        _, _, event_response = self.get("/events", {"X-PhoneLab-Run": token})
        rows = json.loads(event_response)["events"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"]["isTrusted"], True)
        self.assertEqual(rows[0]["source"], "client_reported_browser_event")
        self.assertNotIn("hardware_success", rows[0])
        self.assertEqual(rows[0]["event"]["clientX"], 8)
        self.assertRegex(rows[0]["run_id"], r"^[0-9a-f]{16}$")
        persisted = (Path(self.temp.name) / "test-events" / "events.jsonl").read_text()
        self.assertNotIn(token, persisted)

    def test_pointermove_uses_common_event_schema_and_rejects_extra_pointer_fields(self):
        _, _, page = self.get("/lab?test_id=pointer&seed=3")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        event = {"type": "pointermove", "isTrusted": True, "target": "drag-source",
                 "timeStamp": 10, "clientX": 31, "clientY": 47, "button": -1}
        with self.post("/events", {"test_id": "pointer", "seq": 1, "event": event},
                       {"X-PhoneLab-Run": token}) as response:
            self.assertEqual(response.status, 201)
        unsafe = {**event, "pointerId": 1}
        with self.assertRaises(HTTPError) as error:
            self.post("/events", {"test_id": "pointer", "seq": 2, "event": unsafe},
                      {"X-PhoneLab-Run": token})
        self.assertEqual(error.exception.code, 400)
        _, _, stored = self.get("/events", {"X-PhoneLab-Run": token})
        rows = json.loads(stored)["events"]
        self.assertEqual([row["event"] for row in rows], [event])
        self.assertEqual(self.controller.submitted, [])

    def test_page_geometry_has_strict_passive_schema_and_never_submits_input(self):
        _, _, page = self.get("/lab?test_id=geometry&seed=5")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        event = {
            "type": "page_geometry", "isTrusted": False, "target": "window", "timeStamp": 0,
            "clientX": None, "clientY": None, "button": None,
            "innerWidth": 390, "innerHeight": 844, "visualWidth": 390, "visualHeight": 844,
            "visualOffsetLeft": 0, "visualOffsetTop": 0, "devicePixelRatio": 3,
        }
        with self.post("/events", {"test_id": "geometry", "seq": 1, "event": event},
                       {"X-PhoneLab-Run": token}) as response:
            self.assertEqual(response.status, 201)
        invalid_events = [
            {**event, "isTrusted": True},
            {**event, "extra": "not allowed"},
            {**event, "innerWidth": True},
            {**event, "visualHeight": 0},
            {**event, "visualOffsetLeft": float("nan")},
            {**event, "devicePixelRatio": 0},
            {**event, "clientX": 1},
        ]
        for seq, invalid in enumerate(invalid_events, start=2):
            with self.subTest(seq=seq):
                with self.assertRaises(HTTPError) as caught:
                    self.post("/events", {"test_id": "geometry", "seq": seq, "event": invalid},
                              {"X-PhoneLab-Run": token})
                self.assertEqual(caught.exception.code, 400)
        _, _, raw = self.get("/events", {"X-PhoneLab-Run": token})
        rows = json.loads(raw)["events"]
        self.assertEqual([row["event"] for row in rows], [event])
        self.assertEqual(self.controller.submitted, [])

    def test_event_ingestion_accepts_new_probe_summary_target(self):
        _, _, page = self.get("/lab?test_id=probe&seed=7")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        event = {"type": "click", "isTrusted": True, "target": "probe-summary", "timeStamp": 4,
                 "clientX": None, "clientY": None, "button": 0}
        with self.post("/events", {"test_id": "probe", "seq": 1, "event": event},
                       {"X-PhoneLab-Run": token}) as response:
            self.assertEqual(response.status, 201)

    def test_event_content_policy_rejects_raw_input_and_unknown_fields(self):
        _, _, page = self.get("/lab?test_id=privacy&seed=3")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        unsafe_events = [
            {"type": "input", "isTrusted": True, "target": "test-text", "timeStamp": 15,
             "clientX": None, "clientY": None, "button": None, "textLength": 8,
             "syntheticTextMatch": False, "data": "password", "value": "password"},
            {"type": "keydown", "isTrusted": True, "target": "test-text", "timeStamp": 16,
             "clientX": None, "clientY": None, "button": None, "key": "p"},
            {"type": "click", "isTrusted": True, "target": "01", "timeStamp": 17,
             "clientX": 1, "clientY": 1, "button": 0, "private": "password"},
        ]
        for seq, event in enumerate(unsafe_events, start=1):
            with self.subTest(seq=seq):
                with self.assertRaises(HTTPError) as error:
                    self.post("/events", {"test_id": "privacy", "seq": seq, "event": event},
                              {"X-PhoneLab-Run": token})
                self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.post("/events", {"test_id": "privacy", "seq": 4,
                                   "event": {"type": "click", "isTrusted": True, "target": "01",
                                             "timeStamp": 18, "clientX": 2, "clientY": 2, "button": 0},
                                   "user_data": "password"},
                      {"X-PhoneLab-Run": token})
        self.assertEqual(error.exception.code, 400)
        safe_input = {"type": "input", "isTrusted": True, "target": "test-text", "timeStamp": 19,
                      "clientX": None, "clientY": None, "button": None,
                      "textLength": 8, "syntheticTextMatch": False}
        with self.post("/events", {"test_id": "privacy", "seq": 5, "event": safe_input},
                       {"X-PhoneLab-Run": token}) as response:
            self.assertEqual(response.status, 201)
        _, _, safe_response = self.get("/events", {"X-PhoneLab-Run": token})
        safe_rows = json.loads(safe_response)["events"]
        self.assertEqual(safe_rows[0]["event"], safe_input)
        self.assertNotIn("password", (Path(self.temp.name) / "test-events" / "events.jsonl").read_text())

    def test_only_fixed_synthetic_final_text_is_persisted(self):
        _, _, page = self.get("/lab?test_id=corpus&seed=9")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        corpus = (
            "test123",
            "TEST123",
            "Test 123",
            "test123!@#",
            'ASCII symbols: !"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~',
        )
        for seq, text in enumerate(corpus, start=1):
            event = {"type": "final_state", "isTrusted": True, "text": text,
                     "textLength": len(text), "syntheticTextMatch": True,
                     "scrollX": 0, "scrollY": 0, "dragState": "not-started"}
            with self.post("/events", {"test_id": "corpus", "seq": seq, "event": event},
                           {"X-PhoneLab-Run": token}) as response:
                self.assertEqual(response.status, 201)
        unsafe = {"type": "final_state", "isTrusted": True, "text": "private message",
                  "textLength": 15, "syntheticTextMatch": True,
                  "scrollX": 0, "scrollY": 0, "dragState": "not-started"}
        with self.assertRaises(HTTPError) as error:
            self.post("/events", {"test_id": "corpus", "seq": 6, "event": unsafe},
                      {"X-PhoneLab-Run": token})
        self.assertEqual(error.exception.code, 400)
        _, _, event_response = self.get("/events", {"X-PhoneLab-Run": token})
        rows = json.loads(event_response)["events"]
        self.assertEqual([row["event"]["text"] for row in rows], list(corpus))
        self.assertNotIn("private message", (Path(self.temp.name) / "test-events" / "events.jsonl").read_text())

    def test_event_results_are_scoped_to_the_issuing_run(self):
        _, _, first_page = self.get("/lab?test_id=same&seed=1")
        _, _, second_page = self.get("/lab?test_id=same&seed=1")
        first_token = first_page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        second_token = second_page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        self.assertNotEqual(first_token, second_token)
        event = {"type": "click", "isTrusted": True, "target": "01", "timeStamp": 12,
                 "clientX": 8, "clientY": 9, "button": 0}
        for token in (first_token, second_token):
            with self.post("/events", {"test_id": "same", "seq": 1, "event": event},
                           {"X-PhoneLab-Run": token}) as response:
                self.assertEqual(response.status, 201)
        _, _, first_events = self.get("/events", {"X-PhoneLab-Run": first_token})
        _, _, second_events = self.get("/events", {"X-PhoneLab-Run": second_token})
        first_rows = json.loads(first_events)["events"]
        second_rows = json.loads(second_events)["events"]
        self.assertEqual(len(first_rows), 1)
        self.assertEqual(len(second_rows), 1)
        self.assertNotEqual(first_rows[0]["run_id"], second_rows[0]["run_id"])

    def test_events_endpoint_has_no_cors_and_control_checks_origin_even_with_token(self):
        _, _, page = self.get("/lab?test_id=cors&seed=1")
        token = page.decode().split('"runToken":"', 1)[1].split('"', 1)[0]
        _, event_headers, _ = self.get("/events", {"X-PhoneLab-Run": token})
        self.assertNotIn("Access-Control-Allow-Origin", event_headers)
        with self.assertRaises(HTTPError) as error:
            self.post("/control", {
                "id": "request-1", "session": "session-1", "owner": "owner-1",
                "deadline": time.monotonic() + 2, "op": "click", "args": {}
            }, {
                "Origin": "http://evil.example", "X-PhoneLab-Control": self.server.control_token
            })
        self.assertEqual(error.exception.code, 403)

    def test_wrong_or_missing_run_token_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/events", {"test_id": "x", "seq": 1, "event": {"type": "click"}})
        self.assertEqual(error.exception.code, 403)

    def test_event_body_limit_is_enforced(self):
        request = Request(
            self.base + "/events",
            data=b"{" + b" " * (64 * 1024) + b"}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 413)

    def test_control_requires_local_origin_and_control_header_and_allowlisted_action(self):
        body = {
            "id": "request-1", "session": "session-1", "owner": "owner-1",
            "deadline": time.monotonic() + 2, "op": "click", "args": {}
        }
        with self.assertRaises(HTTPError) as error:
            self.post("/control", body, {"Origin": self.base})
        self.assertEqual(error.exception.code, 403)
        with self.post("/control", body, {
            "Origin": self.base, "X-PhoneLab-Control": self.server.control_token
        }) as response:
            self.assertEqual(json.loads(response.read()), {"accepted": True})
        self.assertEqual(self.controller.submitted, [body])
        with self.assertRaises(HTTPError) as error:
            self.post("/control", {
                "id": "request-2", "session": "session-1", "owner": "owner-1",
                "deadline": time.monotonic() + 2, "op": "shell", "args": {"command": "id"}
            }, {
                "Origin": self.base, "X-PhoneLab-Control": self.server.control_token
            })
        self.assertEqual(error.exception.code, 400)

    def test_control_bounds_deadline_and_arguments_before_controller(self):
        bad_body = {
            "id": "request-1", "session": "session-1", "owner": "owner-1",
            "deadline": time.monotonic() + 30, "op": "move", "args": {"dx": 3000, "dy": 0}
        }
        with self.assertRaises(HTTPError) as error:
            self.post("/control", bad_body, {
                "Origin": self.base, "X-PhoneLab-Control": self.server.control_token
            })
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.controller.submitted, [])

    def test_stop_requires_control_header_and_local_origin(self):
        from phonelab.stop_signal import StopStore
        token = StopStore(Path(self.temp.name) / 'private').capture()
        with self.assertRaises(HTTPError) as error:
            self.post("/stop", {}, {"Origin": self.base})
        self.assertEqual(error.exception.code, 403)
        self.assertFalse(token.is_set())
        with self.post("/stop", {}, {
            "Origin": self.base, "X-PhoneLab-Control": self.server.control_token
        }) as response:
            result = json.loads(response.read())
            self.assertTrue(result['stopped'])
            self.assertEqual(result['shared_stop'], 'published')
            self.assertFalse(result['release_confirmed'])
        self.assertTrue(self.controller.stopped)
        self.assertTrue(token.is_set())

    def test_stop_publishes_before_local_controller_cleanup(self):
        from phonelab.stop_signal import StopStore
        token = StopStore(Path(self.temp.name) / 'private').capture()
        def stop():
            self.assertTrue(token.is_set())
            return {'stopped': True}
        self.controller.stop = stop
        with self.post('/stop', {}, {'Origin': self.base,
                       'X-PhoneLab-Control': self.server.control_token}) as response:
            self.assertEqual(response.status, 200)

    def test_stop_publication_failure_still_stops_local_controller(self):
        class FailedStore:
            def publish_stop(self): raise OSError('private error never exposed')
        self.server.stop_store = FailedStore()
        with self.assertRaises(HTTPError) as caught:
            self.post('/stop', {}, {'Origin': self.base,
                      'X-PhoneLab-Control': self.server.control_token})
        self.assertEqual(caught.exception.code, 503)
        body = json.loads(caught.exception.read())
        self.assertEqual(body['shared_stop'], 'failed')
        self.assertNotIn('private error', str(body))
        self.assertTrue(self.controller.stopped)

    def test_server_refuses_non_loopback_binding(self):
        with self.assertRaises(ValueError):
            create_server(host="0.0.0.0", port=0, data_root=Path(self.temp.name))


if __name__ == "__main__":
    unittest.main()
