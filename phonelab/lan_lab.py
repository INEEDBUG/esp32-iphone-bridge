"""Explicitly approved, restricted LAN projection of the synthetic PhoneLab page.

This module is intentionally not imported by the normal PhoneLab CLI. Creating
the production server requires a matching, explicit firewall-scope approval.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import json
import re
from urllib.parse import parse_qsl, urlsplit


MAX_REQUEST_BODY = 64 * 1024
MAX_RESPONSE_BODY = 2 * 1024 * 1024
MAX_TARGET_BYTES = 2048
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RFC1918 = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
_ALLOWED_LAB_QUERY = {"test_id", "seed"}


def validate_lan_proxy_config(local_address: str, remote_subnet: str, backend_port: int, approval: dict):
    """Validate all runtime and approval gates without opening a socket."""
    if not isinstance(approval, dict):
        raise ValueError("explicit LAN approval is required")
    if (
        approval.get("lan_test_page") is not True
        or approval.get("firewall_scope_verified") is not True
        or approval.get("local_address") != local_address
        or approval.get("remote_subnet") != remote_subnet
    ):
        raise ValueError("explicit LAN approval does not match the requested bind scope")
    if isinstance(backend_port, bool) or not isinstance(backend_port, int) or not 1 <= backend_port <= 65535:
        raise ValueError("invalid loopback backend port")
    try:
        address = ipaddress.ip_address(local_address)
        subnet = ipaddress.ip_network(remote_subnet, strict=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid LAN address or subnet") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not isinstance(subnet, ipaddress.IPv4Network):
        raise ValueError("LAN proxy supports explicit IPv4 RFC1918 scopes only")
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise ValueError("LAN bind address must be a specific RFC1918 interface address")
    if not any(address in block for block in _RFC1918):
        raise ValueError("LAN bind address is not RFC1918")
    if subnet.prefixlen < 24 or not any(subnet.subnet_of(block) for block in _RFC1918):
        raise ValueError("remote subnet must be an RFC1918 /24 or narrower")
    if address not in subnet:
        raise ValueError("bind address must belong to the approved remote subnet")
    return address, subnet


def _make_proxy_handler(backend_port: int, allowed_subnet: ipaddress.IPv4Network):
    """Build a request handler. Kept private so tests can bind only on loopback."""

    class RestrictedProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "PhoneLab-LAN-Projection/0.1"
        sys_version = ""

        def log_message(self, fmt, *args):
            # The path can include test identifiers and the run token is sensitive.
            return

        def _reply(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if status == 405:
                self.send_header("Allow", "GET, POST")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, code: str):
            self._reply(status, json.dumps({"error": code}, separators=(",", ":")).encode("ascii"))

        def _peer_allowed(self) -> bool:
            try:
                peer = ipaddress.ip_address(self.client_address[0])
            except (ValueError, IndexError):
                return False
            return isinstance(peer, ipaddress.IPv4Address) and peer in allowed_subnet

        def _target(self, method: str):
            raw = self.path
            if len(raw.encode("utf-8", errors="replace")) > MAX_TARGET_BYTES or not raw.startswith("/") or raw.startswith("//"):
                return None, 400
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
                return None, 400
            if method == "GET" and parsed.path == "/lab":
                if parsed.query:
                    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.query):
                        return None, 400
                    try:
                        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
                    except ValueError:
                        return None, 400
                    if any(key not in _ALLOWED_LAB_QUERY for key, _ in pairs):
                        return None, 400
                    if len({key for key, _ in pairs}) != len(pairs):
                        return None, 400
                    if any(not value or len(value) > 64 or not value.isascii() for _, value in pairs):
                        return None, 400
                    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for _, value in pairs):
                        return None, 400
                return raw, 200
            if parsed.query:
                return None, 400
            if method == "GET" and parsed.path in {"/events", "/health"}:
                return raw, 200
            if method == "POST" and parsed.path == "/events":
                return raw, 200
            return None, 404

        def _request(self, method: str):
            if not self._peer_allowed():
                self._error(403, "peer_outside_approved_subnet")
                return
            target, target_status = self._target(method)
            if target is None:
                self._error(target_status, "request_not_allowed")
                return

            body = None
            headers = {}
            if method == "POST":
                if self.headers.get("Transfer-Encoding"):
                    self._error(400, "transfer_encoding_not_supported")
                    return
                content_types = self.headers.get_all("Content-Type", [])
                if len(content_types) != 1 or self.headers.get_content_type() != "application/json":
                    self._error(415, "application_json_required")
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    self._error(400, "invalid_content_length")
                    return
                body_length = int(lengths[0])
                if body_length > MAX_REQUEST_BODY:
                    self._error(413, "body_too_large")
                    return
                body = self.rfile.read(body_length)
                if len(body) != body_length:
                    self._error(400, "incomplete_body")
                    return
                headers["Content-Type"] = "application/json"

            if target.endswith("/events"):
                run_headers = self.headers.get_all("X-PhoneLab-Run", [])
                if len(run_headers) > 1:
                    self._error(400, "duplicate_run_token_header")
                    return
                if run_headers:
                    token = run_headers[0]
                    if not _TOKEN_RE.fullmatch(token):
                        self._error(400, "invalid_run_token_header")
                        return
                    headers["X-PhoneLab-Run"] = token

            connection = http.client.HTTPConnection("127.0.0.1", backend_port, timeout=5)
            try:
                connection.request(method, target, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read(MAX_RESPONSE_BODY + 1)
                if len(payload) > MAX_RESPONSE_BODY:
                    self._error(502, "backend_response_too_large")
                    return
                content_type = response.getheader("Content-Type", "application/octet-stream")
                self._reply(response.status, payload, content_type)
            except (OSError, http.client.HTTPException):
                self._error(502, "loopback_backend_unavailable")
            finally:
                connection.close()

        def do_GET(self):
            self._request("GET")

        def do_POST(self):
            self._request("POST")

        def _method_not_allowed(self):
            if not self._peer_allowed():
                self._error(403, "peer_outside_approved_subnet")
            else:
                self._error(405, "method_not_allowed")

        do_HEAD = _method_not_allowed
        do_PUT = _method_not_allowed
        do_PATCH = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_OPTIONS = _method_not_allowed
        do_CONNECT = _method_not_allowed
        do_TRACE = _method_not_allowed

    return RestrictedProxyHandler


def create_lan_proxy(
    local_address: str,
    remote_subnet: str,
    backend_port: int,
    approval: dict,
    port: int = 8767,
) -> ThreadingHTTPServer:
    """Bind an approved LAN interface; the caller owns serving and shutdown."""
    address, subnet = validate_lan_proxy_config(local_address, remote_subnet, backend_port, approval)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid LAN proxy port")
    server = ThreadingHTTPServer((str(address), port), _make_proxy_handler(backend_port, subnet))
    server.daemon_threads = True
    server.allowed_subnet = subnet
    server.backend_port = backend_port
    return server
