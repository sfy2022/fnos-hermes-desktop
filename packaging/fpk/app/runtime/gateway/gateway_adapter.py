#!/usr/bin/env python3
"""Thin fnOS gateway adapter for Hermes Desktop.

The fnOS unified gateway validates the NAS session and forwards
``/app/<app>/*`` requests to a Unix socket in the app's ``target`` directory.
Hermes' Webapp already understands path-prefix deployments through the
``X-Forwarded-Prefix`` header; this adapter only has to:

* strip the gateway prefix,
* forward ``X-Forwarded-Prefix`` (and normalize Host/Origin/Referer),
* relay WebSocket upgrades unchanged,
* relax ONE security directive on the way back:
  ``frame-ancestors 'none'`` -> ``frame-ancestors 'self'``.

It deliberately does **not** rewrite HTML/CSS/JS bodies.  Hermes serves a
strict CSP with sha256 hashes for its inline bootstrap; mutating the body
invalidates those hashes and produces a blank page.

The adapter is stdlib-only and runs on the device's Python 3.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import select
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

VERSION = "1.0.0"
ADAPTER_NAME = "fnos-gateway-adapter"

MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 512 * 1024 * 1024
RECV_BUF = 64 * 1024
UPSTREAM_TIMEOUT = 300.0
WS_IDLE_TIMEOUT = 600.0

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


LOG_FILE = ""


def log(msg: str) -> None:
    line = f"[gateway-adapter] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


DEBUG = os.environ.get("HERMES_ADAPTER_DEBUG") == "1"


def debug(msg: str) -> None:
    if DEBUG:
        log(f"debug: {msg}")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def strip_prefix(path: str, prefix: str) -> str:
    """Strip ``prefix`` from an origin-form path, preserving query/fragment."""
    if not prefix:
        return path
    if not path:
        return "/"
    # Absolute-form targets are possible from some proxies.
    if path.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(path)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    if path.startswith(prefix + "?"):
        return "/" + path[len(prefix):]
    if path.startswith(prefix + "#"):
        return "/" + path[len(prefix):]
    return path


def relax_frame_ancestors(csp: str) -> tuple[str, bool]:
    """Return ``(new_csp, changed)``.

    Only the ``frame-ancestors`` directive is touched.  Everything else,
    including inline-script hashes, stays byte-for-byte intact.
    """
    if not csp:
        return csp, False
    parts = csp.split(";")
    changed = False
    for index, part in enumerate(parts):
        stripped = part.strip()
        if not stripped:
            continue
        name = stripped.split(None, 1)[0].lower()
        if name == "frame-ancestors":
            parts[index] = " frame-ancestors 'self'"
            changed = True
    return ";".join(parts), changed


def relax_x_frame_options(value: str) -> str:
    if value.strip().upper() in {"DENY", "SAMEORIGIN"}:
        return "SAMEORIGIN"
    return value


def get_header(headers: list[tuple[str, str]], name: str) -> str:
    lname = name.lower()
    for key, value in headers:
        if key.lower() == lname:
            return value
    return ""


def set_header(headers: list[tuple[str, str]], name: str, value: str) -> None:
    lname = name.lower()
    headers[:] = [(k, v) for k, v in headers if k.lower() != lname]
    headers.append((name, value))


def remove_header(headers: list[tuple[str, str]], name: str) -> None:
    lname = name.lower()
    headers[:] = [(k, v) for k, v in headers if k.lower() != lname]


def is_websocket_request(headers: list[tuple[str, str]]) -> bool:
    upgrade = get_header(headers, "upgrade").lower()
    connection = get_header(headers, "connection").lower()
    return upgrade == "websocket" or ("upgrade" in connection and "websocket" in upgrade)


def _rewrite_absolute_url(value: str, upstream_authority: str) -> str:
    """Rewrite scheme://host[:port] to ``http://<upstream_authority>``."""
    if not value:
        return value
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    return urllib.parse.urlunsplit(
        ("http", upstream_authority, parsed.path, parsed.query, parsed.fragment)
    )


def build_upstream_headers(
    headers: list[tuple[str, str]],
    *,
    prefix: str,
    upstream_authority: str,
    forwarded_for: str = "127.0.0.1",
) -> list[tuple[str, str]]:
    """Build request headers for the upstream Hermes process."""
    out: list[tuple[str, str]] = []
    for key, value in headers:
        lkey = key.lower()
        if lkey in HOP_BY_HOP or lkey in {"host", "content-length", "x-forwarded-prefix"}:
            continue
        if lkey in {"origin", "referer"}:
            value = _rewrite_absolute_url(value, upstream_authority)
        out.append((key, value))

    out.append(("Host", upstream_authority))
    out.append(("X-Forwarded-Prefix", prefix))
    out.append(("X-Forwarded-Host", get_header(headers, "host") or upstream_authority))
    out.append(("X-Forwarded-Proto", get_header(headers, "x-forwarded-proto") or "http"))
    existing_xff = get_header(headers, "x-forwarded-for")
    out.append(("X-Forwarded-For", f"{existing_xff}, {forwarded_for}" if existing_xff else forwarded_for))
    return out


def rewrite_response_headers(headers: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
    """Strip hop-by-hop headers, relax frame policy, force Connection: close."""
    out: list[tuple[str, str]] = []
    csp_changed = False
    for key, value in headers:
        lkey = key.lower()
        if lkey in {"connection", "keep-alive", "transfer-encoding"}:
            continue
        if lkey == "content-security-policy":
            value, changed = relax_frame_ancestors(value)
            csp_changed = csp_changed or changed
        elif lkey == "x-frame-options":
            value = relax_x_frame_options(value)
        out.append((key, value))
    out.append(("Connection", "close"))
    return out, csp_changed


# ---------------------------------------------------------------------------
# HTTP parsing
# ---------------------------------------------------------------------------


class RequestError(Exception):
    def __init__(self, code: int, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.code = code
        self.reason = reason


def _recv_more(sock: socket.socket, buf: bytearray) -> None:
    chunk = sock.recv(RECV_BUF)
    if not chunk:
        raise RequestError(400, "Bad Request", "client closed connection")
    buf.extend(chunk)


def _read_until(sock: socket.socket, buf: bytearray, delimiter: bytes, limit: int) -> tuple[bytes, bytes]:
    while delimiter not in buf:
        if len(buf) > limit:
            raise RequestError(431, "Request Header Fields Too Large")
        _recv_more(sock, buf)
    head, _, rest = bytes(buf).partition(delimiter)
    return head, rest


def _read_chunked_body(sock: socket.socket, initial: bytes) -> bytes:
    buf = bytearray(initial)
    body = bytearray()
    while True:
        while b"\r\n" not in buf:
            _recv_more(sock, buf)
        line, _, rest = bytes(buf).partition(b"\r\n")
        buf = bytearray(rest)
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise RequestError(400, "Bad Request", "invalid chunk size") from exc
        if size == 0:
            # Consume trailers until the terminating empty line.
            while b"\r\n\r\n" not in buf and b"\r\n" not in buf:
                _recv_more(sock, buf)
            return bytes(body)
        if len(body) + size > MAX_BODY_BYTES:
            raise RequestError(413, "Payload Too Large")
        while len(buf) < size + 2:
            _recv_more(sock, buf)
        body.extend(buf[:size])
        del buf[: size + 2]


def read_request(sock: socket.socket) -> tuple[str, str, str, list[tuple[str, str]], bytes]:
    """Read one HTTP request (headers + body).  Returns method, target, version,
    headers, body."""
    buf = bytearray()
    head, rest = _read_until(sock, buf, b"\r\n\r\n", MAX_HEADER_BYTES)
    try:
        text = head.decode("latin-1")
        lines = text.split("\r\n")
        method, target, version = lines[0].split(" ", 2)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RequestError(400, "Bad Request", "malformed request line") from exc

    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise RequestError(400, "Bad Request", "malformed header")
        key, _, value = line.partition(":")
        headers.append((key.strip(), value.strip()))

    body = b""
    transfer_encoding = get_header(headers, "transfer-encoding").lower()
    content_length = get_header(headers, "content-length")
    if "chunked" in transfer_encoding:
        body = _read_chunked_body(sock, rest)
    elif content_length:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise RequestError(400, "Bad Request", "invalid content-length") from exc
        if length > MAX_BODY_BYTES:
            raise RequestError(413, "Payload Too Large")
        body = bytearray(rest)
        while len(body) < length:
            _recv_more(sock, body)
        body = bytes(body[:length])
    return method, target, version, headers, body


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def send_simple_response(
    sock: socket.socket,
    code: int,
    reason: str,
    payload: bytes = b"",
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    if not payload:
        payload = f"{code} {reason}\n".encode()
    head = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    try:
        sock.sendall(head + payload)
    except OSError:
        pass


def _upstream_is_alive(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Proxy server
# ---------------------------------------------------------------------------


class GatewayHandler(socketserver.BaseRequestHandler):
    server: "ProxyServer"

    def handle(self) -> None:
        try:
            method, target, version, headers, body = read_request(self.request)
        except RequestError as exc:
            send_simple_response(self.request, exc.code, exc.reason, str(exc).encode())
            return
        except OSError:
            return

        prefix = self.server.prefix
        path_only, sep, query = target.partition("?")
        stripped = strip_prefix(path_only, prefix)

        # Health endpoint is handled locally (never forwarded, never authenticated here;
        # the fnOS gateway already validated the NAS session for this route).
        if stripped == self.server.health_path:
            self._health()
            return

        upstream_target = stripped + (sep + query if sep else "")
        forwarded_for = self.client_address[0] if self.client_address else "127.0.0.1"
        try:
            if is_websocket_request(headers):
                self._proxy_websocket(upstream_target, headers, body)
            else:
                self._proxy_http(method, upstream_target, headers, body, forwarded_for)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # keep the socket alive for other requests
            log(f"request failed: {method} {target}: {exc}")
            try:
                send_simple_response(self.request, 502, "Bad Gateway")
            except Exception:
                pass

    # -- local endpoints ---------------------------------------------------

    def _health(self) -> None:
        host, port = self.server.upstream
        payload = json.dumps(
            {
                "ok": True,
                "adapter": ADAPTER_NAME,
                "version": VERSION,
                "pid": os.getpid(),
                "prefix": self.server.prefix,
                "upstream": f"{host}:{port}",
                "upstream_ok": _upstream_is_alive(host, port),
                "socket": self.server.socket_path,
            },
            ensure_ascii=False,
        ).encode()
        send_simple_response(self.request, 200, "OK", payload, "application/json; charset=utf-8")

    # -- HTTP --------------------------------------------------------------

    def _proxy_http(
        self,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes,
        forwarded_for: str,
    ) -> None:
        host, port = self.server.upstream
        out_headers = build_upstream_headers(
            headers,
            prefix=self.server.prefix,
            upstream_authority=f"{host}:{port}",
            forwarded_for=forwarded_for,
        )
        if body:
            out_headers.append(("Content-Length", str(len(body))))

        conn = http.client.HTTPConnection(host, port, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            for key, value in out_headers:
                conn.putheader(key, value)
            conn.endheaders(body or None)
            response = conn.getresponse()

            response_headers, _ = rewrite_response_headers(list(response.getheaders()))
            status_line = f"HTTP/1.1 {response.status} {response.reason}\r\n"
            head = status_line + "".join(f"{k}: {v}\r\n" for k, v in response_headers) + "\r\n"
            self.request.sendall(head.encode("latin-1"))

            if method.upper() == "HEAD" or response.status in (204, 304):
                return
            # Stream the body: never buffer SSE, chat streaming or large downloads.
            # ``read1`` returns available bytes (chunked responses would otherwise
            # block in ``read(n)`` until n bytes or EOF are collected).
            read_chunk = getattr(response, "read1", response.read)
            while True:
                chunk = read_chunk(RECV_BUF)
                if not chunk:
                    break
                self.request.sendall(chunk)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception:
            raise
        finally:
            conn.close()

    # -- WebSocket ---------------------------------------------------------

    def _proxy_websocket(
        self,
        target: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        host, port = self.server.upstream
        authority = f"{host}:{port}"
        out_headers = build_upstream_headers(
            headers,
            prefix=self.server.prefix,
            upstream_authority=authority,
        )
        # Keep the upgrade handshake fields that build_upstream_headers stripped.
        out_headers.append(("Upgrade", "websocket"))
        out_headers.append(("Connection", "Upgrade"))

        upstream = socket.create_connection((host, port), timeout=30)
        try:
            request_lines = [f"GET {target} HTTP/1.1"]
            for key, value in out_headers:
                request_lines.append(f"{key}: {value}")
            upstream.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1"))
            if body:
                upstream.sendall(body)

            # Relay the handshake response verbatim (101 or an upstream error).
            buf = bytearray()
            while b"\r\n\r\n" not in buf:
                chunk = upstream.recv(RECV_BUF)
                if not chunk:
                    raise ConnectionError("upstream closed during WebSocket handshake")
                buf.extend(chunk)
                if len(buf) > MAX_HEADER_BYTES:
                    raise ConnectionError("upstream handshake headers too large")
            head, _, rest = bytes(buf).partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0]
            self.request.sendall(head + b"\r\n\r\n")
            if rest:
                self.request.sendall(rest)
            debug(f"ws handshake status={status!r} rest={len(rest)}")
            if b" 101 " not in status:
                return

            # Raw frame relay. The proxy never parses frames, so extensions negotiated
            # between browser and upstream (permessage-deflate) keep working.
            clients = [self.request, upstream]
            last_activity = time.monotonic()
            while True:
                readable, _, errored = select.select(clients, [], clients, 1.0)
                if errored:
                    debug("ws errored")
                    return
                if not readable:
                    if time.monotonic() - last_activity > WS_IDLE_TIMEOUT:
                        return
                    continue
                last_activity = time.monotonic()
                for source in readable:
                    try:
                        data = source.recv(RECV_BUF)
                    except OSError as exc:
                        debug(f"ws recv error: {exc}")
                        return
                    if not data:
                        debug("ws eof")
                        return
                    debug(f"ws relay {len(data)} bytes from {'client' if source is self.request else 'upstream'}")
                    destination = upstream if source is self.request else self.request
                    try:
                        destination.sendall(data)
                    except OSError as exc:
                        debug(f"ws send error: {exc}")
                        return
        finally:
            try:
                upstream.close()
            except OSError:
                pass


if hasattr(socketserver, "UnixStreamServer"):

    class ProxyServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 64

        def __init__(
            self,
            socket_path: str,
            handler,
            *,
            prefix: str,
            upstream: tuple[str, int],
            health_path: str,
        ) -> None:
            self.prefix = prefix.rstrip("/")
            self.upstream = upstream
            self.health_path = health_path
            self.socket_path = socket_path
            try:
                if os.path.exists(socket_path):
                    os.unlink(socket_path)
            except OSError as exc:
                log(f"cannot remove stale socket {socket_path}: {exc}")
            super().__init__(socket_path, handler)
            try:
                os.chmod(socket_path, 0o755)
            except OSError as exc:
                log(f"cannot chmod socket {socket_path}: {exc}")

        def server_close(self) -> None:
            super().server_close()
            try:
                if os.path.exists(self.socket_path):
                    os.unlink(self.socket_path)
            except OSError:
                pass

else:  # Windows dev hosts: pure helpers stay importable; the adapter runs on fnOS/Linux.
    ProxyServer = None  # type: ignore[assignment]



def serve(
    *,
    socket_path: str,
    upstream: tuple[str, int],
    prefix: str,
    health_path: str,
) -> int:
    if ProxyServer is None:
        log("Unix stream sockets are not available on this platform; adapter must run on fnOS Linux")
        return 2
    server = ProxyServer(
        socket_path,
        GatewayHandler,
        prefix=prefix,
        upstream=upstream,
        health_path=health_path,
    )

    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    log(f"listening on {socket_path} -> {upstream[0]}:{upstream[1]} prefix={prefix or '(none)'}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    global LOG_FILE
    parser = argparse.ArgumentParser(description="fnOS thin gateway adapter for Hermes Desktop")
    parser.add_argument("--socket", required=True, help="Unix socket path to listen on")
    parser.add_argument("--upstream", required=True, help="upstream host:port")
    parser.add_argument("--prefix", default="", help="gateway prefix to strip (e.g. /app/hermes-desktop)")
    parser.add_argument("--health-path", default="/.gateway-health", help="local health endpoint path")
    parser.add_argument("--log-file", default="", help="append adapter logs to this file as well")
    parser.add_argument("--version", action="version", version=f"{ADAPTER_NAME} {VERSION}")
    args = parser.parse_args(argv)

    if args.log_file:
        LOG_FILE = args.log_file
    host, _, port_text = args.upstream.rpartition(":")
    if not host or not port_text.isdigit():
        log(f"invalid --upstream {args.upstream!r}; expected host:port")
        return 2
    return serve(
        socket_path=args.socket,
        upstream=(host, int(port_text)),
        prefix=args.prefix,
        health_path=args.health_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
