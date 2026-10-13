import http.client
import ipaddress
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from phonelab.lan_lab import (
    MAX_REQUEST_BODY,
    _make_proxy_handler,
    create_lan_proxy,
    validate_lan_proxy_config,
)
from phonelab.web import create_server


class LanProxyValidationTests(unittest.TestCase):
    def approval(self, address="192.168.1.20", subnet="192.168.1.0/24"):
        return {
            "lan_test_page": True,
            "firewall_scope_verified": True,
            "local_address": address,
            "remote_subnet": subnet,
        }

    def test_requires_all_explicit_approval_fields_to_match(self):
        valid = self.approval()
        self.assertEqual(
            validate_lan_proxy_config("192.168.1.20", "192.168.1.0/24", 8766, valid),
            (ipaddress.ip_address("192.168.1.20"), ipaddress.ip_network("192.168.1.0/24")),
        )
        for changes in (
            {"lan_test_page": 1},
            {"lan_test_page": False},
            {"firewall_scope_verified": False},
            {"local_address": "192.168.1.21"},
            {"remote_subnet": "192.168.2.0/24"},
        ):
            with self.subTest(changes=changes):
                approval = valid | changes
                with self.assertRaises(ValueError):
                    validate_lan_proxy_config("192.168.1.20", "192.168.1.0/24", 8766, approval)

    def test_rejects_wildcard_public_malformed_broad_and_nonmatching_networks(self):
        for address, subnet in (
            ("0.0.0.0", "192.168.1.0/24"),
            ("127.0.0.1", "127.0.0.0/8"),
            ("203.0.113.8", "203.0.113.0/24"),
            ("192.168.1.20", "192.168.0.0/16"),
            ("192.168.1.20", "192.168.2.0/24"),
            ("192.168.1.20", "192.168.1.1/24"),
        ):
            with self.subTest(address=address, subnet=subnet):
                with self.assertRaises(ValueError):
                    validate_lan_proxy_config(address, subnet, 8766, self.approval(address, subnet))

    def test_rejects_bad_backend_port(self):
        for port in (0, -1, 65536, True):
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    validate_lan_proxy_config("192.168.1.20", "192.168.1.0/24", port, self.approval())

    def test_factory_refuses_without_approval_before_attempting_any_bind(self):
        with patch("phonelab.lan_lab.ThreadingHTTPServer") as server_type:
            with self.assertRaises(ValueError):
                create_lan_proxy("192.168.1.20", "192.168.1.0/24", 8766, {})
            server_type.assert_not_called()


class LanProxyHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.backend = create_server("127.0.0.1", 0, Path(cls.temp.name), None)
        cls.proxy = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _make_proxy_handler(cls.backend.server_port, ipaddress.ip_network("127.0.0.0/8")),
        )
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
        cls.backend_thread.start()
        cls.proxy_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.backend.shutdown()
        cls.proxy.server_close()
        cls.backend.server_close()
        cls.proxy_thread.join(timeout=2)
        cls.backend_thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, method, target, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=3)
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_allows_only_lab_events_health_and_safe_lab_query(self):
        status, page, _ = self.request("GET", "/lab?test_id=smoke_1&seed=3")
        self.assertEqual(status, 200)
        self.assertIn(b"PhoneLab", page)

        status, body, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "offline_not_real_phone")

        status, _, _ = self.request("GET", "/lab?test_id=one&admin=true")
        self.assertEqual(status, 400)
        status, _, _ = self.request("GET", "/lab?test_id=one&test_id=two")
        self.assertEqual(status, 400)

    def test_allows_sanitized_run_scoped_event_post_and_read(self):
        _, page, _ = self.request("GET", "/lab?test_id=proxytest&seed=7")
        marker = b'"runToken":"'
        token = page.split(marker, 1)[1].split(b'"', 1)[0].decode("ascii")
        headers = {"X-PhoneLab-Run": token, "Content-Type": "application/json; charset=utf-8"}
        event = {"test_id": "proxytest", "seq": 1, "event": {
            "type": "click", "isTrusted": True, "target": "01", "timeStamp": 12,
            "clientX": 8, "clientY": 9, "button": 0,
        }}
        status, body, _ = self.request("POST", "/events", json.dumps(event), headers)
        self.assertEqual(status, 201)
        self.assertTrue(json.loads(body)["stored"])
        status, body, _ = self.request("GET", "/events", headers={"X-PhoneLab-Run": token})
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["events"]), 1)

    def test_blocks_control_stop_status_screen_unknown_methods_and_absolute_targets(self):
        for method, target in (
            ("POST", "/control"),
            ("POST", "/stop"),
            ("GET", "/status"),
            ("GET", "/screen"),
            ("GET", "/observations"),
            ("GET", "/arbitrary"),
            ("GET", "http://127.0.0.1/lab"),
            ("GET", "//127.0.0.1/lab"),
            ("PUT", "/events"),
        ):
            with self.subTest(method=method, target=target):
                status, _, _ = self.request(method, target, body="{}" if method == "POST" else None,
                                            headers={"Content-Type": "application/json"})
                self.assertIn(status, (400, 404, 405))

    def test_rejects_query_on_non_lab_and_caps_post_body_before_read(self):
        status, _, _ = self.request("GET", "/health?x=1")
        self.assertEqual(status, 400)
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=3)
        connection.putrequest("POST", "/events")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_REQUEST_BODY + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        response.read()
        connection.close()

    def test_rejects_wrong_content_type_and_repeated_run_header(self):
        event = json.dumps({"test_id": "x", "seq": 1, "event": {}})
        status, _, _ = self.request("POST", "/events", event, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=3)
        connection.putrequest("GET", "/events")
        connection.putheader("X-PhoneLab-Run", "a")
        connection.putheader("X-PhoneLab-Run", "b")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()

    def test_peer_subnet_is_checked_on_each_request(self):
        handler_type = _make_proxy_handler(8766, ipaddress.ip_network("192.168.1.0/24"))
        class Peer:
            client_address = ("192.168.1.99", 1234)
        class Outsider:
            client_address = ("192.168.2.99", 1234)
        class InvalidPeer:
            client_address = ("::1", 1234)
        self.assertTrue(handler_type._peer_allowed(Peer()))
        self.assertFalse(handler_type._peer_allowed(Outsider()))
        self.assertFalse(handler_type._peer_allowed(InvalidPeer()))


if __name__ == "__main__":
    unittest.main()
