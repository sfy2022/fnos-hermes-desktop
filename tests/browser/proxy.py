#!/usr/bin/env python3
"""同源回放夹具：让 tests/browser/fake-gateway.html 与目标网关处于同一 origin。

背景：适配层只把响应头 CSP 的 ``frame-ancestors 'none'`` 放宽为 ``'self'``
（见 docs/REFACTOR-CONTRACT.md §2/§5），因此 **父页面必须与 iframe 同源**，
否则浏览器仍会按规范拒绝内嵌 —— 用 file:// 或另一个端口直接打开夹具时
看到的 "Refused to display" 是预期行为，不能用来判定适配层好坏。

本脚本是一个纯标准库的本地反向代理：

* ``/tests/browser/*`` 由本地磁盘提供（夹具页面本身）；
* 其余路径原样转发到 ``--target`` 指向的飞牛统一网关（默认 http://192.168.1.2:5666）；
* 转发时改写 ``Host``、``Origin``、``Referer``，让上游认为请求来自网关本身；
* 把上游返回的绝对跳转（``Location``）改写到本地代理 origin，避免登录流程跳出代理；
* 转发 WebSocket 升级（101 后做裸字节双向透传），便于观察 WS 101；
* 对 ``Set-Cookie`` 去掉 ``Domain``/``Secure`` 属性，使浏览器能把登录态留在本地代理 origin。

于是浏览器看到的是：``http://127.0.0.1:8090/tests/browser/fake-gateway.html``
（父页面）和 ``http://127.0.0.1:8090/app/hermes-desktop``（iframe）—— 两者同源，
``frame-ancestors 'self'`` 可以被真实浏览器执行并验证。

用法::

    python tests/browser/proxy.py --target http://192.168.1.2:5666
    # 浏览器打开：
    # http://127.0.0.1:8090/tests/browser/fake-gateway.html?url=/app/hermes-desktop

注意：这只是**人工验证辅助工具**，不进入 fpk 包；远端 HTTPS 自签证书可用
``--insecure`` 跳过校验。生产环境不要运行它，也不要把它监听在 0.0.0.0。
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import os
import select
import socket
import ssl
import sys
import urllib.parse

FIXTURE_PREFIX = "/tests/browser/"
DEFAULT_TARGET = "http://192.168.1.2:5666"
DEFAULT_PORT = 8090
DEFAULT_LISTEN = "127.0.0.1"

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

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}

# WebSocket 握手需要原样透传的请求头（Origin/Referer 单独改写）。
WS_PASSTHROUGH_HEADERS = {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-protocol",
    "sec-websocket-extensions",
    "cookie",
    "authorization",
    "user-agent",
    "accept-language",
}


def log(message: str) -> None:
    sys.stderr.write("[fixture-proxy] " + message + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 纯函数（便于人工检查与复用）
# ---------------------------------------------------------------------------


def replace_origin(value: str, target_origin: str) -> str:
    """把头部值里的 scheme://host:port 换成目标 origin，保留 path/query/fragment。"""
    try:
        parts = urllib.parse.urlsplit(value)
        target = urllib.parse.urlsplit(target_origin)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc or not target.scheme or not target.netloc:
        return value
    return urllib.parse.urlunsplit(
        (target.scheme, target.netloc, parts.path, parts.query, parts.fragment)
    )


def rewrite_location(value: str, client_origin: str, target_origin: str) -> str:
    """把上游绝对 Location 改写到本地代理 origin，避免跳回设备地址。"""
    if value.startswith(target_origin):
        return client_origin + value[len(target_origin):]
    return value


def relax_set_cookie(value: str) -> str:
    """去掉 Set-Cookie 的 Domain/Secure 属性，让 Cookie 能留在本地代理 origin。"""
    kept = []
    for attribute in value.split(";"):
        stripped = attribute.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if lowered.startswith("domain=") or lowered == "secure":
            continue
        kept.append(stripped)
    return "; ".join(kept)


def is_websocket_upgrade(headers) -> bool:
    upgrade = (headers.get("Upgrade") or "").lower()
    connection = (headers.get("Connection") or "").lower()
    return upgrade == "websocket" or ("upgrade" in connection and "websocket" in upgrade)


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------


class FixtureServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, target: str, fixture_dir: str,
                 relax_cookies: bool, insecure_tls: bool):
        super().__init__(address, handler)
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise SystemExit("--target 必须是 http(s)://host[:port]，收到：" + target)
        self.target = parsed
        self.target_origin = parsed.scheme + "://" + parsed.netloc
        self.target_host = parsed.hostname
        self.target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.fixture_dir = os.path.abspath(fixture_dir)
        self.relax_cookies = relax_cookies
        self.tls_context = None
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            if insecure_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self.tls_context = context


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fnos-fixture-proxy"

    def log_message(self, fmt, *args):  # noqa: A003 - BaseHTTPRequestHandler API
        log("%s %s" % (self.command, self.path))

    # -- 入口 ------------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def _dispatch(self):
        try:
            if self.path.startswith(FIXTURE_PREFIX):
                self._serve_fixture()
                return
            if is_websocket_upgrade(self.headers):
                self._tunnel_websocket()
                return
            self._proxy_http()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - 人工排障路径
            log("代理异常：%r" % (exc,))
            try:
                self.send_error(502, "fixture proxy error")
            except Exception:
                pass

    # -- 本地夹具静态文件 --------------------------------------------------
    def _serve_fixture(self):
        relative = urllib.parse.unquote(
            urllib.parse.urlsplit(self.path).path[len(FIXTURE_PREFIX):]
        )
        if relative in ("", "/"):
            self.send_response(302)
            self.send_header("Location", FIXTURE_PREFIX + "fake-gateway.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        normalized = os.path.normpath(relative).replace("\\", "/")
        full_path = os.path.abspath(os.path.join(self.server.fixture_dir, normalized))
        within_root = os.path.commonpath([full_path, self.server.fixture_dir]) == self.server.fixture_dir
        if not within_root or not os.path.isfile(full_path):
            self.send_error(404, "fixture file not found")
            return

        with open(full_path, "rb") as handle:
            payload = handle.read()
        ext = os.path.splitext(full_path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    # -- 反向代理 HTTP ------------------------------------------------------
    def _upstream_connection(self):
        server = self.server
        timeout = 120
        if server.target.scheme == "https":
            return http.client.HTTPSConnection(
                server.target_host, server.target_port, timeout=timeout,
                context=server.tls_context,
            )
        return http.client.HTTPConnection(server.target_host, server.target_port, timeout=timeout)

    def _request_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _client_origin(self) -> str:
        host = self.headers.get("Host") or "%s:%d" % self.server.server_address
        return "http://" + host

    def _forward_headers(self):
        headers = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP or lowered in ("host", "content-length"):
                continue
            if lowered in ("origin", "referer"):
                value = replace_origin(value, self.server.target_origin)
            headers[name] = value
        headers["Host"] = self.server.target.netloc
        return headers

    def _proxy_http(self):
        server = self.server
        client_origin = self._client_origin()
        body = self._request_body()
        connection = self._upstream_connection()
        try:
            connection.request(self.command, self.path, body=body,
                               headers=self._forward_headers())
            response = connection.getresponse()

            self.send_response_only(response.status, response.reason)
            content_length = response.getheader("Content-Length")
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in HOP_BY_HOP or lowered == "content-length":
                    continue
                if lowered == "set-cookie" and server.relax_cookies:
                    value = relax_set_cookie(value)
                elif lowered in ("location", "content-location"):
                    value = rewrite_location(value, client_origin, server.target_origin)
                self.send_header(name, value)
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            self.send_header("Connection", "close")
            self.end_headers()

            if self.command != "HEAD":
                self.wfile.flush()
                while True:
                    chunk = response.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        finally:
            connection.close()
            self.close_connection = True

    # -- WebSocket 升级透传 -------------------------------------------------
    def _tunnel_websocket(self):
        server = self.server
        upstream = socket.create_connection((server.target_host, server.target_port), timeout=15)
        if server.tls_context is not None:
            upstream = server.tls_context.wrap_socket(upstream, server_hostname=server.target_host)
        try:
            head_lines = [
                "%s %s HTTP/1.1" % (self.command, self.path),
                "Host: " + server.target.netloc,
                "Upgrade: websocket",
                "Connection: Upgrade",
            ]
            for name, value in self.headers.items():
                if name.lower() in WS_PASSTHROUGH_HEADERS and value:
                    head_lines.append("%s: %s" % (name, value))
            origin = self.headers.get("Origin")
            if origin:
                head_lines.append("Origin: " + replace_origin(origin, server.target_origin))
            referer = self.headers.get("Referer")
            if referer:
                head_lines.append("Referer: " + replace_origin(referer, server.target_origin))

            upstream.sendall(("\r\n".join(head_lines) + "\r\n\r\n").encode("latin-1"))

            response_head = b""
            while b"\r\n\r\n" not in response_head:
                chunk = upstream.recv(8192)
                if not chunk:
                    break
                response_head += chunk
                if len(response_head) > 65536:
                    break
            if not response_head:
                self.send_error(502, "empty upstream websocket response")
                return
            self.connection.sendall(response_head)
            self.close_connection = True
            status_line = response_head.split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                log("WS 升级被上游拒绝：" + status_line.decode("latin-1", "replace"))
                return

            self._tunnel(upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _tunnel(self, upstream: socket.socket):
        client = self.connection
        client.setblocking(True)
        upstream.setblocking(True)
        idle_seconds = 120
        while True:
            try:
                readable, _, _ = select.select([client, upstream], [], [], idle_seconds)
            except (OSError, ValueError):
                return
            if not readable:
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                destination = upstream if source is client else client
                destination.sendall(data)


def make_server(listen: str, port: int, target: str, fixture_dir: str,
                relax_cookies: bool = True, insecure_tls: bool = False) -> FixtureServer:
    return FixtureServer(
        (listen, port), FixtureHandler, target=target, fixture_dir=fixture_dir,
        relax_cookies=relax_cookies, insecure_tls=insecure_tls,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="同源回放夹具：为 tests/browser/fake-gateway.html 提供本地反向代理。"
    )
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="飞牛统一网关地址（默认 %(default)s）")
    parser.add_argument("--listen", default=DEFAULT_LISTEN, help="监听地址（默认 %(default)s）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 %(default)s）")
    parser.add_argument("--fixture-dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="夹具静态文件目录（默认本脚本所在目录）")
    parser.add_argument("--no-relax-cookies", action="store_true",
                        help="不修改 Set-Cookie（默认去掉 Domain/Secure，便于本地保留登录态）")
    parser.add_argument("--insecure", action="store_true",
                        help="跳过 HTTPS 目标证书校验（自签证书时使用）")
    args = parser.parse_args(argv)

    server = make_server(
        listen=args.listen, port=args.port, target=args.target,
        fixture_dir=args.fixture_dir, relax_cookies=not args.no_relax_cookies,
        insecure_tls=args.insecure,
    )
    origin = "http://%s:%d" % (args.listen, args.port)
    log("目标网关：%s" % server.target_origin)
    log("夹具地址：%s%s?url=/app/hermes-desktop" % (origin, FIXTURE_PREFIX + "fake-gateway.html"))
    log("按 Ctrl+C 停止。仅用于人工验证，不要用于生产。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
