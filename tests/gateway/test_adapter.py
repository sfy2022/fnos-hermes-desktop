#!/usr/bin/env python3
"""Contract tests for the thin fnOS gateway adapter.

Two layers:

* **Pure helpers** run on every platform (Windows included): prefix stripping,
  CSP / X-Frame-Options relaxation, header build/rewrite.
* **Unix-socket integration** runs on Linux CI only.  A fake upstream built on
  ``http.server.ThreadingHTTPServer`` returns a strict-CSP page, redirects,
  chunked SSE, large bodies, HEAD/204/304 and a minimal WebSocket echo; the
  adapter runs in-process on a temporary AF_UNIX socket.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ADAPTER_PATH = (
    REPO / "packaging" / "fpk" / "app" / "runtime" / "gateway"
    / "gateway_adapter.py"
)
PREFIX = "/app/hermes-desktop"
UPSTREAM_AUTHORITY = "127.0.0.1:9120"

BODY = (
    b"<!doctype html><html><head>"
    b"<script>window.__HERMES_BASE_PATH__='/app/hermes-desktop'</script>"
    b"</head><body>hermes</body></html>"
)
CSP = (
    "default-src 'self'; frame-ancestors 'none'; "
    "script-src 'sha256-abc123' 'sha256-def456'; "
    "base-uri 'none'; form-action 'none'"
)


def load_adapter():
    spec = importlib.util.spec_from_file_location(
        "hermes_gateway_adapter", ADAPTER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Never leave ``packaging/fpk/.../__pycache__`` behind: the layout gate
    # treats it as package junk.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


adapter = load_adapter()
HAS_UNIX = (
    os.name == "posix"
    and hasattr(socket, "AF_UNIX")
    and hasattr(socketserver, "UnixStreamServer")
    and adapter.ProxyServer is not None
)


def temp_dir(prefix: str) -> Path:
    path = Path(tempfile.gettempdir()) / (prefix + uuid.uuid4().hex)
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# Pure helper tests (all platforms)
# ---------------------------------------------------------------------------


class PureHelperTests(unittest.TestCase):
    def test_strip_prefix_strict_boundary(self) -> None:
        strip = adapter.strip_prefix
        self.assertEqual(strip(PREFIX, PREFIX), "/")
        self.assertEqual(strip(PREFIX + "/", PREFIX), "/")
        self.assertEqual(strip(PREFIX + "/api/x", PREFIX), "/api/x")
        self.assertEqual(strip(PREFIX + "/api/x?q=1", PREFIX), "/api/x?q=1")
        self.assertEqual(strip(PREFIX + "?q=1", PREFIX), "/?q=1")
        self.assertEqual(strip(PREFIX + "#frag", PREFIX), "/#frag")
        # Strict boundary: must NOT match a longer path sharing the prefix.
        self.assertEqual(
            strip(PREFIX + "x/api", PREFIX), PREFIX + "x/api"
        )
        self.assertEqual(strip("/other", PREFIX), "/other")
        self.assertEqual(strip("", PREFIX), "/")

    def test_strip_prefix_empty_prefix(self) -> None:
        self.assertEqual(adapter.strip_prefix("/x?y=1", ""), "/x?y=1")
        # Empty prefix is a no-op; the gateway always supplies a prefix.
        self.assertEqual(adapter.strip_prefix("", ""), "")

    def test_relax_frame_ancestors_only_that_directive(self) -> None:
        new_csp, changed = adapter.relax_frame_ancestors(CSP)
        self.assertTrue(changed)
        self.assertIn("frame-ancestors 'self'", new_csp)
        self.assertNotIn("frame-ancestors 'none'", new_csp)
        # Inline-script hashes and every other directive stay byte-identical.
        self.assertIn("sha256-abc123", new_csp)
        self.assertIn("sha256-def456", new_csp)
        self.assertIn("base-uri 'none'", new_csp)
        self.assertIn("form-action 'none'", new_csp)
        old_directives = [part.strip() for part in CSP.split(";")]
        new_directives = [part.strip() for part in new_csp.split(";")]
        old_rest = [d for d in old_directives
                    if not d.lower().startswith("frame-ancestors")]
        new_rest = [d for d in new_directives
                    if not d.lower().startswith("frame-ancestors")]
        self.assertEqual(old_rest, new_rest)

    def test_relax_frame_ancestors_noop_when_absent(self) -> None:
        csp = "default-src 'self'; script-src 'self'"
        new_csp, changed = adapter.relax_frame_ancestors(csp)
        self.assertFalse(changed)
        self.assertEqual(new_csp, csp)
        self.assertEqual(adapter.relax_frame_ancestors(""), ("", False))

    def test_relax_x_frame_options(self) -> None:
        self.assertEqual(adapter.relax_x_frame_options("DENY"), "SAMEORIGIN")
        self.assertEqual(adapter.relax_x_frame_options("deny"), "SAMEORIGIN")
        self.assertEqual(
            adapter.relax_x_frame_options("SAMEORIGIN"), "SAMEORIGIN"
        )
        self.assertEqual(
            adapter.relax_x_frame_options("ALLOW-FROM x"), "ALLOW-FROM x"
        )

    def test_build_upstream_headers(self) -> None:
        headers = [
            ("Host", "desktop.local:5666"),
            ("Origin", "http://desktop.local:5666"),
            ("Referer", "http://desktop.local:5666/app/hermes-desktop/"),
            ("Cookie", "session=abc"),
            ("Authorization", "Bearer token"),
            ("X-Trim-User", "sun"),
            ("Sec-WebSocket-Key", "key-1"),
            ("Connection", "keep-alive, Upgrade"),
            ("Upgrade", "websocket"),
            ("Content-Length", "5"),
            ("X-Forwarded-Prefix", "/wrong"),
            ("X-Forwarded-Proto", "https"),
        ]
        out = adapter.build_upstream_headers(
            headers,
            prefix=PREFIX,
            upstream_authority=UPSTREAM_AUTHORITY,
            forwarded_for="10.0.0.9",
        )
        values = {key.lower(): value for key, value in out}
        self.assertEqual(values["host"], UPSTREAM_AUTHORITY)
        self.assertEqual(values["x-forwarded-prefix"], PREFIX)
        self.assertEqual(values["origin"], f"http://{UPSTREAM_AUTHORITY}")
        self.assertTrue(
            values["referer"].startswith(f"http://{UPSTREAM_AUTHORITY}/")
        )
        self.assertEqual(values["cookie"], "session=abc")
        self.assertEqual(values["authorization"], "Bearer token")
        self.assertEqual(values["x-trim-user"], "sun")
        self.assertEqual(values["sec-websocket-key"], "key-1")
        self.assertEqual(values["x-forwarded-proto"], "https")
        self.assertIn("10.0.0.9", values["x-forwarded-for"])
        # Hop-by-hop / rewritten-from-scratch headers must not leak through.
        for gone in ("connection", "upgrade", "content-length"):
            self.assertNotIn(gone, values)

    def test_rewrite_response_headers(self) -> None:
        headers = [
            ("Content-Security-Policy", CSP),
            ("X-Frame-Options", "DENY"),
            ("Location", "http://127.0.0.1:9120/login"),
            ("Set-Cookie", "session=abc; Path=/; HttpOnly"),
            ("Connection", "keep-alive"),
            ("Transfer-Encoding", "chunked"),
            ("Content-Type", "text/html; charset=utf-8"),
        ]
        out, changed = adapter.rewrite_response_headers(list(headers))
        self.assertTrue(changed)
        values = {key.lower(): value for key, value in out}
        self.assertIn("frame-ancestors 'self'",
                      values["content-security-policy"])
        self.assertIn("sha256-abc123", values["content-security-policy"])
        self.assertNotIn("frame-ancestors 'none'",
                         values["content-security-policy"])
        self.assertEqual(values["x-frame-options"], "SAMEORIGIN")
        # The adapter must never rewrite these (upstream X-Forwarded-Prefix
        # handling is responsible for making them correct).
        self.assertEqual(
            values["location"], "http://127.0.0.1:9120/login"
        )
        self.assertEqual(
            values["set-cookie"], "session=abc; Path=/; HttpOnly"
        )
        self.assertEqual(values["content-type"], "text/html; charset=utf-8")
        self.assertNotIn("transfer-encoding", values)
        # Connection: close is appended (the upstream's hop-by-hop headers are
        # stripped); check the raw list too.
        self.assertEqual(values["connection"], "close")
        self.assertEqual(out[-1], ("Connection", "close"))

    def test_rewrite_response_headers_no_csp(self) -> None:
        out, changed = adapter.rewrite_response_headers(
            [("X-Frame-Options", "SAMEORIGIN")]
        )
        self.assertFalse(changed)
        values = {key.lower(): value for key, value in out}
        self.assertEqual(values["x-frame-options"], "SAMEORIGIN")

    def test_header_helpers(self) -> None:
        headers = [("A", "1"), ("B", "2")]
        self.assertEqual(adapter.get_header(headers, "a"), "1")
        adapter.set_header(headers, "A", "9")
        self.assertEqual(adapter.get_header(headers, "a"), "9")
        adapter.remove_header(headers, "b")
        self.assertEqual(adapter.get_header(headers, "b"), "")

    def test_is_websocket_request(self) -> None:
        self.assertTrue(adapter.is_websocket_request(
            [("Upgrade", "websocket")]
        ))
        self.assertTrue(adapter.is_websocket_request(
            [("Connection", "keep-alive, Upgrade"), ("Upgrade", "websocket")]
        ))
        self.assertFalse(adapter.is_websocket_request(
            [("Connection", "keep-alive")]
        ))

    def test_rewrite_absolute_url(self) -> None:
        self.assertEqual(
            adapter._rewrite_absolute_url(
                "http://desktop.local:5666/x?y=1", UPSTREAM_AUTHORITY
            ),
            f"http://{UPSTREAM_AUTHORITY}/x?y=1",
        )
        self.assertEqual(
            adapter._rewrite_absolute_url("/relative", UPSTREAM_AUTHORITY),
            "/relative",
        )

    def test_cli_help_and_version(self) -> None:
        help_run = subprocess.run(
            [sys.executable, str(ADAPTER_PATH), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(help_run.returncode, 0)
        for flag in ("--socket", "--upstream", "--prefix", "--health-path"):
            self.assertIn(flag, help_run.stdout)
        version_run = subprocess.run(
            [sys.executable, str(ADAPTER_PATH), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(version_run.returncode, 0)
        self.assertIn("1.0.0", version_run.stdout)
        bad = subprocess.run(
            [sys.executable, str(ADAPTER_PATH), "--upstream", "bad"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        self.assertNotEqual(bad.returncode, 0)


class ReadRequestTests(unittest.TestCase):
    """Request parser tests using ``socket.socketpair`` (works on Windows)."""

    def _socket_pair(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return left, right

    def test_chunked_body_decoded(self) -> None:
        left, right = self._socket_pair()
        right.sendall(
            b"POST /echo HTTP/1.1\r\n"
            b"Host: desktop.local\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        method, target, _version, headers, body = adapter.read_request(left)
        self.assertEqual(method, "POST")
        self.assertEqual(target, "/echo")
        self.assertEqual(body, b"hello world")
        self.assertEqual(adapter.get_header(headers, "host"), "desktop.local")

    def test_content_length_body(self) -> None:
        left, right = self._socket_pair()
        right.sendall(
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
        )
        _method, _target, _version, _headers, body = adapter.read_request(left)
        self.assertEqual(body, b"hello")

    def test_header_limit_rejected(self) -> None:
        left, right = self._socket_pair()
        right.sendall(
            b"GET / HTTP/1.1\r\nHost: x\r\n"
            + (b"X-Padding: " + b"a" * 100 + b"\r\n") * 700
        )
        with self.assertRaises(adapter.RequestError) as context:
            adapter.read_request(left)
        self.assertEqual(context.exception.code, 431)

    def test_body_limit_rejected(self) -> None:
        left, right = self._socket_pair()
        right.sendall(
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: "
            + str(adapter.MAX_BODY_BYTES + 1).encode()
            + b"\r\n\r\n"
        )
        with self.assertRaises(adapter.RequestError) as context:
            adapter.read_request(left)
        self.assertEqual(context.exception.code, 413)

    def test_malformed_request_rejected(self) -> None:
        left, right = self._socket_pair()
        right.sendall(b"NOT-HTTP\r\n\r\n")
        with self.assertRaises(adapter.RequestError) as context:
            adapter.read_request(left)
        self.assertEqual(context.exception.code, 400)


# ---------------------------------------------------------------------------
# Fake upstream + integration (Linux CI only)
# ---------------------------------------------------------------------------

if HAS_UNIX:
    import base64
    import http.client  # noqa: F401  (documented dependency of adapter)
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    class FakeUpstreamState:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.requests: list = []

        def record(self, method: str, path: str, headers, body: bytes) -> None:
            with self.lock:
                self.requests.append(
                    {
                        "method": method,
                        "path": path,
                        "headers": [(key, value) for key, value in headers],
                        "body": body,
                    }
                )

        def last(self) -> dict:
            with self.lock:
                return self.requests[-1] if self.requests else {}

        def count(self) -> int:
            with self.lock:
                return len(self.requests)

    def make_handler(state: FakeUpstreamState):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            # -- helpers -------------------------------------------------
            def _record(self, method: str, body: bytes = b"") -> None:
                state.record(method, self.path, self.headers.items(), body)

            def _send(self, code: int, headers, body: bytes = b"",
                      *, head_only: bool = False) -> None:
                self.send_response(code)
                for key, value in headers:
                    self.send_header(key, value)
                if code not in (204, 304):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body and not head_only:
                    self.wfile.write(body)
                    self.wfile.flush()

            # -- routes --------------------------------------------------
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/ws" and (
                    self.headers.get("Upgrade", "").lower() == "websocket"
                ):
                    self._record("GET")
                    self._websocket()
                    return
                self._record("GET")
                if path == "/":
                    self._send(
                        200,
                        [
                            ("Content-Type", "text/html; charset=utf-8"),
                            ("Content-Security-Policy", CSP),
                            ("X-Frame-Options", "DENY"),
                            ("Set-Cookie",
                             "session=abc; Path=/; HttpOnly; SameSite=Lax"),
                        ],
                        BODY,
                    )
                elif path == "/redirect":
                    self._send(
                        302,
                        [
                            ("Location", "http://127.0.0.1:9120/login"),
                            ("Set-Cookie", "route=redirect; Path=/"),
                        ],
                        b"",
                    )
                elif path == "/sse":
                    self._sse()
                elif path == "/large":
                    self._send(
                        200,
                        [("Content-Type", "application/octet-stream")],
                        b"x" * (1024 * 1024),
                    )
                elif path == "/empty":
                    self._send(204, [])
                elif path == "/notmodified":
                    self._send(304, [])
                else:
                    self._send(404, [("Content-Type", "text/plain")],
                               b"not found")

            def do_HEAD(self) -> None:
                self._record("HEAD")
                self._send(
                    200,
                    [("Content-Type", "text/html; charset=utf-8")],
                    BODY,
                    head_only=True,
                )

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length)
                self._record("POST", body)
                path = self.path.split("?", 1)[0]
                if path == "/echo":
                    self._send(
                        200,
                        [("Content-Type", "application/octet-stream")],
                        b"echo:" + body,
                    )
                else:
                    self._send(404, [], b"not found")

            # -- special routes -----------------------------------------
            def _sse(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for part in (b"event: one\ndata: 1\n\n",
                             b"event: two\ndata: 2\n\n"):
                    self.wfile.write(
                        f"{len(part):x}\r\n".encode() + part + b"\r\n"
                    )
                    self.wfile.flush()
                    time.sleep(0.8)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _websocket(self) -> None:
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(
                    hashlib.sha1((key + WS_GUID).encode()).digest()
                ).decode()
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()

                def read_exact(length: int):
                    data = bytearray()
                    while len(data) < length:
                        chunk = self.rfile.read(length - len(data))
                        if not chunk:
                            return None
                        data.extend(chunk)
                    return bytes(data)

                # A real WebSocket echo server unmasks the client frame and
                # answers with an unmasked text frame.  Echoing the raw masked
                # bytes (the old behaviour) is not a valid server frame.
                while True:
                    try:
                        head = read_exact(2)
                        if head is None:
                            return
                        length = head[1] & 0x7F
                        if length == 126:
                            ext = read_exact(2)
                            if ext is None:
                                return
                            length = struct.unpack(">H", ext)[0]
                        elif length == 127:
                            ext = read_exact(8)
                            if ext is None:
                                return
                            length = struct.unpack(">Q", ext)[0]
                        mask = read_exact(4) if head[1] & 0x80 else b""
                        if mask is None:
                            return
                        payload = read_exact(length) if length else b""
                        if payload is None:
                            return
                        if mask:
                            payload = bytes(
                                byte ^ mask[index % 4]
                                for index, byte in enumerate(payload)
                            )
                        if length < 126:
                            header = bytes([0x81, length])
                        elif length < 65536:
                            header = bytes([0x81, 126]) + struct.pack(">H", length)
                        else:
                            header = bytes([0x81, 127]) + struct.pack(">Q", length)
                        self.wfile.write(header + payload)
                        self.wfile.flush()
                    except (OSError, ValueError):
                        return

        return Handler

    class UnixSocketServer:
        def __init__(self, socket_path: Path, upstream_port: int) -> None:
            self.socket_path = socket_path
            self.server = adapter.ProxyServer(
                str(socket_path),
                adapter.GatewayHandler,
                prefix=PREFIX,
                upstream=("127.0.0.1", upstream_port),
                health_path="/.gateway-health",
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )

        def start(self) -> None:
            self.thread.start()

        def stop(self) -> None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

    class FakeUpstream:
        def __init__(self) -> None:
            self.state = FakeUpstreamState()
            self.httpd = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(self.state)
            )
            self.httpd.daemon_threads = True
            self.thread = threading.Thread(
                target=self.httpd.serve_forever, daemon=True
            )

        @property
        def port(self) -> int:
            return self.httpd.server_address[1]

        def start(self) -> None:
            self.thread.start()

        def stop(self) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)

    def unix_request(socket_path: Path, raw: bytes, *, timeout: float = 5.0,
                     keep_open: bool = False):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.sendall(raw)
        chunks = []
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if keep_open:
                    break
        except socket.timeout:
            pass
        finally:
            sock.close()
        return b"".join(chunks)

    def parse_response(raw: bytes):
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        headers = []
        for line in lines[1:]:
            if b":" in line:
                key, _, value = line.partition(b":")
                headers.append((key.decode("latin-1"),
                                value.decode("latin-1").strip()))
        return status, headers, body

    def header_values(headers, name: str) -> list:
        return [value for key, value in headers
                if key.lower() == name.lower()]

    def get_header(headers, name: str) -> str:
        values = header_values(headers, name)
        return values[0] if values else ""

    def recv_exact(sock: socket.socket, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data.extend(chunk)
        return bytes(data)

    def client_ws_frame(payload: bytes) -> bytes:
        mask = b"\x01\x02\x03\x04"
        masked = bytes(
            byte ^ mask[index % 4] for index, byte in enumerate(payload)
        )
        return bytes([0x81, 0x80 | len(payload)]) + mask + masked

    def read_server_ws_frame(sock: socket.socket) -> bytes:
        first = recv_exact(sock, 2)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", recv_exact(sock, 8))[0]
        return recv_exact(sock, length)


@unittest.skipUnless(HAS_UNIX, "Unix socket integration tests require Linux")
class AdapterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.upstream = FakeUpstream()
        cls.upstream.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.upstream.stop()

    def setUp(self) -> None:
        self.tmp = temp_dir("fpk-gw-")
        self.socket_path = self.tmp / "app.sock"
        self.server = UnixSocketServer(self.socket_path, self.upstream.port)
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()
        for path in self.tmp.iterdir():
            try:
                path.unlink()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def request(self, method: str, path: str, headers=None,
                body: bytes = b"", *, timeout: float = 5.0) -> bytes:
        base_headers = {
            "Origin": "http://desktop.local:5666",
            "Referer": "http://desktop.local:5666/app/hermes-desktop/",
        }
        if headers:
            base_headers.update(headers)
        lines = [f"{method} {path} HTTP/1.1",
                 "Host: desktop.local:5666",
                 "Connection: close"]
        for key, value in base_headers.items():
            lines.append(f"{key}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
        return unix_request(self.socket_path, raw, timeout=timeout)

    # ----------------------------------------------------------- HTTP/body
    def test_html_body_unchanged_and_csp_only_relaxed(self) -> None:
        raw = self.request(
            "GET",
            PREFIX + "/",
            headers={
                "Cookie": "session=abc",
                "Authorization": "Bearer token",
                "X-Trim-User": "sun",
            },
        )
        status, headers, body = parse_response(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body, BODY)
        csp = get_header(headers, "Content-Security-Policy")
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("sha256-abc123", csp)
        self.assertNotIn("frame-ancestors 'none'", csp)
        self.assertEqual(get_header(headers, "X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("session=abc", get_header(headers, "Set-Cookie"))

        upstream_request = self.upstream.state.last()
        upstream_headers = dict(
            (key.lower(), value) for key, value in upstream_request["headers"]
        )
        self.assertEqual(upstream_request["path"], "/")
        self.assertEqual(
            upstream_headers.get("x-forwarded-prefix"), PREFIX
        )
        self.assertEqual(
            upstream_headers.get("host"), f"127.0.0.1:{self.upstream.port}"
        )
        self.assertEqual(
            upstream_headers.get("origin"),
            f"http://127.0.0.1:{self.upstream.port}",
        )
        self.assertTrue(
            upstream_headers.get("referer", "").startswith(
                f"http://127.0.0.1:{self.upstream.port}/"
            )
        )
        self.assertEqual(upstream_headers.get("cookie"), "session=abc")
        self.assertEqual(
            upstream_headers.get("authorization"), "Bearer token"
        )
        self.assertEqual(upstream_headers.get("x-trim-user"), "sun")

    def test_redirect_location_and_cookie_untouched(self) -> None:
        raw = self.request("GET", PREFIX + "/redirect")
        status, headers, _body = parse_response(raw)
        self.assertEqual(status, 302)
        self.assertEqual(
            get_header(headers, "Location"),
            "http://127.0.0.1:9120/login",
        )
        self.assertEqual(
            get_header(headers, "Set-Cookie"), "route=redirect; Path=/"
        )

    def test_path_prefix_boundary_not_stripped(self) -> None:
        raw = self.request("GET", PREFIX + "x/")
        status, _headers, _body = parse_response(raw)
        self.assertEqual(status, 404)
        self.assertEqual(
            self.upstream.state.last()["path"], PREFIX + "x/"
        )

    def test_head_has_no_body(self) -> None:
        raw = self.request("HEAD", PREFIX + "/")
        status, headers, body = parse_response(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(
            get_header(headers, "Content-Length"), str(len(BODY))
        )

    def test_204_and_304_have_no_body(self) -> None:
        for path, expected in (("/empty", 204), ("/notmodified", 304)):
            raw = self.request("GET", PREFIX + path)
            status, _headers, body = parse_response(raw)
            self.assertEqual(status, expected)
            self.assertEqual(body, b"")

    def test_large_body_streamed_equal(self) -> None:
        raw = self.request("GET", PREFIX + "/large", timeout=15.0)
        status, _headers, body = parse_response(raw)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1024 * 1024)
        self.assertEqual(body, b"x" * (1024 * 1024))

    def test_chunked_request_body_decoded(self) -> None:
        raw = (
            f"POST {PREFIX}/echo HTTP/1.1\r\n"
            "Host: desktop.local:5666\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        raw += b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        response = unix_request(self.socket_path, raw)
        status, _headers, body = parse_response(response)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"echo:hello world")
        self.assertEqual(self.upstream.state.last()["body"], b"hello world")
        headers = dict(
            (key.lower(), value)
            for key, value in self.upstream.state.last()["headers"]
        )
        self.assertEqual(headers.get("content-length"), "11")

    def test_sse_first_event_is_streamed(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(str(self.socket_path))
        sock.sendall((
            f"GET {PREFIX}/sse HTTP/1.1\r\n"
            "Host: desktop.local:5666\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1"))
        started = time.monotonic()
        first_event_at = None
        combined = bytearray()
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            combined.extend(chunk)
            if first_event_at is None and b"event: one" in combined:
                first_event_at = time.monotonic() - started
        sock.close()
        self.assertIn(b"event: one", combined)
        self.assertIn(b"event: two", combined)
        self.assertIsNotNone(first_event_at)
        # Streaming: event one must arrive before the upstream's second chunk
        # (which sleeps 0.8s).  A fully buffered implementation would deliver
        # everything after ~1.2s.
        self.assertLess(first_event_at, 0.5)

    def test_header_limit_rejected(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(str(self.socket_path))
        try:
            sock.sendall(
                f"GET {PREFIX}/ HTTP/1.1\r\nHost: desktop.local\r\n".encode()
                + (b"X-Padding: " + b"a" * 100 + b"\r\n") * 600
            )
        except OSError:
            pass  # adapter may reject while we are still writing
        try:
            response = sock.recv(4096)
        except socket.timeout:
            response = b""
        sock.close()
        self.assertIn(b"431", response.split(b"\r\n", 1)[0])

    def test_body_limit_rejected_via_content_length(self) -> None:
        raw = (
            f"POST {PREFIX}/echo HTTP/1.1\r\n"
            "Host: desktop.local:5666\r\n"
            f"Content-Length: {adapter.MAX_BODY_BYTES + 1}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        response = unix_request(self.socket_path, raw)
        status, _headers, _body = parse_response(response)
        self.assertEqual(status, 413)

    def test_health_endpoint_local(self) -> None:
        before = self.upstream.state.count()
        raw = self.request("GET", PREFIX + "/.gateway-health")
        status, headers, body = parse_response(raw)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["prefix"], PREFIX)
        self.assertEqual(self.upstream.state.count(), before)

    # ----------------------------------------------------------- WebSocket
    def test_websocket_upgrade_and_echo(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(str(self.socket_path))
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        sock.sendall((
            f"GET {PREFIX}/ws HTTP/1.1\r\n"
            "Host: desktop.local:5666\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("latin-1"))
        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head.extend(chunk)
        self.assertIn(b" 101 ", head.split(b"\r\n", 1)[0])
        sock.sendall(client_ws_frame(b"hello"))
        payload = read_server_ws_frame(sock)
        sock.close()
        self.assertEqual(payload, b"hello")


if __name__ == "__main__":
    unittest.main()
