#!/usr/bin/env python3
"""
fnOS 统一网关适配层 —— HTTP over Unix Socket 代理

背景
----
fnOS 的桌面应用内嵌窗口（app/ui/config 的 type: "iframe"）有两种接入方式：

1. 直连模式：声明 protocol + port，浏览器直接访问 主机:端口
   → 端口暴露在局域网，且脱离 fnOS 的登录态与统一网关。

2. 统一网关模式（fnos-dsh 等官方推荐）：声明 gatewayPrefix + gatewaySocket，
   fnOS 网关把 请求前缀 转发到应用目录下的 Unix Socket。

本应用采用第 2 种。fnOS 网关只认 Unix Socket，因此需要一个把
Unix Socket 上的 HTTP 转发到上游 HTTP 服务的中间层。

本脚本用 Python 标准库实现该中间层，零第三方依赖（不需要 Node/npm）：

    fnOS 网关 (:5666)
        │  /app/hermes-desktop/*
        ▼
    Unix Socket  $TRIM_APPDEST/app.sock
        │
        ▼
    本脚本（剥离前缀 + 转发）
        │
        ▼
    Hermes gateway  127.0.0.1:<port>

关键处理
--------
* 路径前缀剥离：上游收到的应是原始路径（/、/api/...），否则路由 404。
* WebSocket 升级：Hermes 的实时通道依赖它，必须原样透传。
* Cookie Path 收窄：上游若发 Set-Cookie: Path=/，会污染 NAS 根路径的 Cookie，
  这里改写为应用挂载目录。
* SSE 心跳：长连接场景下定期发注释行，防止中间层空闲断开。
* 认证 Cookie 透传：fnOS 网关注入的会话标识需要原样带给上游。

用法
----
    python3 scripts/gateway_proxy.py \
        --socket /var/apps/hermes-desktop/target/app.sock \
        --upstream 127.0.0.1:9120 \
        --prefix /app/hermes-desktop
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from urllib.parse import urlsplit
import http.server
import os
import re
import socket
import socketserver
import sys
import threading
import threading
import time
from typing import Optional

SOCK_BACKLOG = 64
RECV_BUF = 65536


def log(msg: str) -> None:
    print(f"[gateway-proxy] {msg}", flush=True)


# 前缀桥接脚本：把前端的站点根绝对路径（fetch/XHR/WS）改写成带网关前缀的路径。
# 从同目录的 fnos_bridge.py 载入（随包一起分发）。
def _load_bridge() -> str:
    try:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "fnos_bridge", os.path.join(here, "fnos_bridge.py"))
        if spec is None or spec.loader is None:
            return ""
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 返回未替换的模板，由调用方填入实际前缀
        return mod.BRIDGE_JS
    except Exception as exc:  # pragma: no cover
        print(f"[gateway-proxy] 载入 bridge 失败：{exc}", flush=True)
        return ""


# ── 内容改写 ─────────────────────────────────────────────────────────────
# 应用挂在 /app/hermes-desktop 子路径下，浏览器请求的 HTML/CSS/JS 里
# 若出现以 / 开头的绝对路径，必须补上网关前缀，否则 404。
# 相对路径（./assets/x.js）依赖 <base> 正确解析。

_HTML_ABS_RE = re.compile(r'''(\b(?:src|href|action)=["'])(/(?!/)[^"']*)''', re.IGNORECASE)
_CSS_ABS_RE = re.compile(r'''url\(\s*(["']?)(/(?!/)[^)"']+)\1\s*\)''', re.IGNORECASE)


def _add_prefix(path: str, prefix: str) -> str:
    if not prefix or path.startswith(prefix):
        return path
    return prefix + path


def rewrite_html(body: bytes, prefix: str) -> bytes:
    html = body.decode("utf-8", "replace")

    # 1) 绝对路径补前缀
    html = _HTML_ABS_RE.sub(
        lambda m: m.group(1) + _add_prefix(m.group(2), prefix), html)

    # 设置 Hermes 自己的子路径基准。
    #
    # 前端所有路由/跳转都基于 window.__HERMES_BASE_PATH__：
    #   fetch(basePath + '/auth/password-login', ...)
    #   location.href = basePath + '/'
    # 上游默认注入空串（站点根），在网关子路径下就会跳到 fnOS 根 ——
    # 表现正是「登录成功后跳到飞牛桌面」。
    #
    # 在这里直接改成网关前缀，前端的所有跳转自然带上 /app/hermes-desktop，
    # 不必去 hook location.href（那属性在多数浏览器里无法覆盖 setter）。
    if prefix:
        html = re.sub(
            r'__HERMES_BASE_PATH__\s*=\s*(""|\'\'|"[^"]*"|\'[^\']*\')',
            f'__HERMES_BASE_PATH__ = "{prefix}"',
            html)

    # launch 页面从 URL 的 fragment 里读凭据（location.hash.slice(1)）。
    # 浏览器跟随 302 重定向时会丢弃 Location 里的 fragment，所以本层把
    # 凭据改用 query 传递（见 _launch_path），这里相应地让页面改读
    # location.search。两者都读，兼容直接打开原始 URL 的情形。
    if "location.hash.slice(1)" in html:
        html = html.replace(
            "location.hash.slice(1)",
            "(location.search || location.hash).slice(1)")
        html = html.replace(
            "location.hash.slice(1)",
            "(location.search || location.hash).slice(1)")

    # 注入前缀桥接脚本：前端的绝对路径 API 调用必须加前缀才能到达本应用。
    # 放在主 bundle 之前，才能在应用代码运行前 hook 住 fetch/XHR/WebSocket。
    if prefix and "__FNOS_BRIDGE__" not in html:
        bridge = _load_bridge().replace("__PREFIX__", prefix)
        if bridge:
            tag = ('<!-- fnos-gateway-prefix-bridge -->\n'
                   '    <script>\n' + bridge + '\n    </script>\n')
            m = re.search(r"\s*<script type=\"module\"", html)
            if m:
                html = html[:m.start()] + "\n" + tag + html[m.start():]
            elif re.search(r"<head\b[^>]*>", html, re.IGNORECASE):
                html = re.sub(r"(<head\b[^>]*>)",
                              lambda mm: mm.group(1) + "\n" + tag, html, count=1,
                              flags=re.IGNORECASE)
            else:
                html = tag + html

    # 2) 注入或修正 base 标签，让相对路径（./assets/...）落在前缀下。
    #    放在 bridge 注入之后：bridge 脚本内容里可能出现 "<base" 字样，
    #    先处理 base 会误把注释里的文本当成真标签。
    base = f'<base href="{prefix}/">' if prefix else '<base href="/">'
    base_tag = re.compile(r"<base\s[^>]*>", re.IGNORECASE)
    if base_tag.search(html):
        html = base_tag.sub(base, html, count=1)
    elif re.search(r"<head\b[^>]*>", html, re.IGNORECASE):
        html = re.sub(r"(<head\b[^>]*>)",
                      lambda m: m.group(1) + base, html, count=1,
                      flags=re.IGNORECASE)
    else:
        html = base + html

    return html.encode("utf-8")


def rewrite_css(body: bytes, prefix: str) -> bytes:
    text = body.decode("utf-8", "replace")
    text = _CSS_ABS_RE.sub(
        lambda m: f"url({m.group(1)}{_add_prefix(m.group(2), prefix)}{m.group(1)})",
        text)
    return text.encode("utf-8")


_JS_REL_FROM_RE = re.compile(r"""(\bfrom\s*["'])(\.\./)""")
_JS_BARE_ASSETS_RE = re.compile(r"""(["'])(assets/)(?=(?:langs/|vendor-|fonts/))""")


def rewrite_javascript(body: bytes, path: str, prefix: str) -> bytes:
    """改写 JS 里的相对导入，使其在网关前缀下仍能解析。

    打包产物中同级 chunk 用 `from "./x.js"`（没问题），跨目录用
    `from "../x.js"` —— 在子路径下会指向错误位置，需要按目录层级
    重新计算前缀。
    """
    src = body.decode("utf-8", "replace")
    if not path.startswith("/assets/"):
        return body

    current_dir = path.rsplit("/", 1)[0] + "/" if "/" in path else "/"
    parent_dir = current_dir
    while parent_dir.endswith("../"):
        parent_dir = parent_dir[:-3]
    parent_dir = parent_dir.rstrip("/") + "/" if parent_dir != "/" else "/"

    current_base = _add_prefix(current_dir, prefix)
    parent_base = _add_prefix(parent_dir, prefix)

    src = _JS_REL_FROM_RE.sub(lambda m: m.group(1) + parent_base, src)
    src = _JS_BARE_ASSETS_RE.sub(
        lambda m: m.group(1) + _add_prefix("/" + m.group(2), prefix), src)
    return src.encode("utf-8")


def should_rewrite(path: str, content_type: str) -> str:
    """返回 'html' / 'css' / '' 表示需要哪种改写。"""
    ctype = (content_type or "").lower()
    if "text/html" in ctype:
        return "html"
    if "text/css" in ctype:
        return "css"
    # 兜底：按扩展名判断（部分上游不返回正确的 content-type）
    if path.endswith((".html", ".htm")):
        return "html"
    if path.endswith(".css"):
        return "css"
    return ""


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """在 Unix Socket 上提供 HTTP 服务。"""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = SOCK_BACKLOG

    def __init__(self, socket_path: str, handler_cls, prefix: str, upstream: tuple[str, int]):
        self.socket_path = socket_path
        self.prefix = prefix.rstrip("/")
        self.upstream_host, self.upstream_port = upstream
        # 先移除残留的 socket 文件，否则 bind 会失败
        try:
            if os.path.exists(socket_path):
                os.unlink(socket_path)
        except OSError as exc:
            log(f"移除旧 socket 失败（忽略）：{exc}")
        super().__init__(socket_path, handler_cls)
        # 权限：与 fnOS 官方微应用参考实现一致（755）。
        # fnOS 网关以 root 身份连接 socket，755 足够；
        # 属主保持为应用用户，便于 fnOS 校验 socket 与应用是否匹配。
        try:
            os.chmod(socket_path, 0o755)
        except OSError as exc:
            log(f"设置 socket 权限失败（忽略）：{exc}")

    def server_close(self) -> None:
        super().server_close()
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass


class ProxyHandler(socketserver.BaseRequestHandler):
    """把单个 HTTP 请求转发给上游。

    注意：Unix Socket 上的 HTTP 服务不能用 BaseHTTPRequestHandler 的
    rfile/wfile 抽象（那是给 TCP 套接字准备的，会先做一次长度预读，
    在 socket 场景下容易与客户端的实际发送节奏错位）。这里直接用
    socket 收发，按 HTTP/1.1 的报文边界自行解析。
    """

    server: "ProxyUnixServer"

    # ── 报文解析 ──────────────────────────────────────────────────────
    def _read_request(self) -> Optional[tuple[str, str, str, dict, bytes]]:
        """读取一个完整的 HTTP 请求，返回 (method, path, version, headers, body)。"""
        buf = b""
        while b"\r\n\r\n" not in buf:
            try:
                chunk = self.request.recv(RECV_BUF)
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 1 << 20:      # 头部过大，放弃
                return None

        head, _, rest = buf.partition(b"\r\n\r\n")
        try:
            lines = head.decode("latin-1").split("\r\n")
        except Exception:
            return None
        if not lines or not lines[0]:
            return None

        parts = lines[0].split(" ")
        if len(parts) < 3:
            return None
        method, path, version = parts[0], parts[1], parts[2]

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()

        # 读 body
        body = rest
        length = headers.get("Content-Length")
        if length:
            try:
                n = int(length)
            except ValueError:
                n = 0
            while len(body) < n:
                try:
                    chunk = self.request.recv(RECV_BUF)
                except OSError:
                    break
                if not chunk:
                    break
                body += chunk
            body = body[:n]

        return method, path, version, headers, body

    # ── 路径处理 ──────────────────────────────────────────────────────
    def _strip_prefix(self, path: str) -> str:
        prefix = self.server.prefix
        if prefix and path.startswith(prefix):
            rest = path[len(prefix):]
            return rest if rest.startswith("/") else "/" + rest
        return path or "/"

    # ── 响应输出 ──────────────────────────────────────────────────────
    def _proxy_websocket(self, target: str, headers: dict[str, str],
                         body: bytes) -> None:
        """裸 socket 双向转发 WebSocket。

        BaseHTTPRequestHandler 帮我们解析了握手请求，但握手成功之后
        双方发的是帧流而不是 HTTP，所以这里必须绕开 http.client，
        直接对接客户端 socket 与上游 socket。
        """
        import select as _select

        up_host, up_port = self.server.upstream_host, self.server.upstream_port
        upstream_origin = f"http://{up_host}:{up_port}"

        lines = [f"GET {target} HTTP/1.1", f"Host: {up_host}:{up_port}"]
        for key, value in headers.items():
            low = key.lower()
            if low in ("host", "content-length"):
                continue
            if low == "origin":
                lines.append(f"Origin: {upstream_origin}")
                continue
            if low == "referer":
                tail = value.split("://", 1)[-1]
                slash = tail.find("/")
                rest = tail[slash:] if slash >= 0 else "/"
                lines.append(f"Referer: {upstream_origin}{rest}")
                continue
            lines.append(f"{key}: {value}")
        handshake = ("\r\n".join(lines) + "\r\n\r\n").encode()

        try:
            up = socket.create_connection((up_host, up_port), timeout=30)
        except OSError as exc:
            log(f"WebSocket 上游连接失败 {up_host}:{up_port}: {exc}")
            self._error(502, "Bad Gateway")
            return

        try:
            up.sendall(handshake)
            if body:
                up.sendall(body)

            # 把上游的握手响应（应为 101）原样回给客户端
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = up.recv(4096)
                if not chunk:
                    break
                head += chunk
            if not head:
                log("WebSocket 上游未返回握手响应")
                self._error(502, "Bad Gateway")
                return
            self.wfile.write(head)
            self.wfile.flush()

            # 之后是帧流：双向对拷
            client = self.connection
            client.setblocking(False)
            up.setblocking(False)
            peers = {client: up, up: client}
            idle = 0
            while True:
                try:
                    readable, _, errored = _select.select(
                        [client, up], [], [client, up], 1.0)
                except OSError:
                    break
                if errored:
                    break
                if not readable:
                    idle += 1
                    # 长时间无数据也维持连接（浏览器的 WS 会有心跳）
                    if idle > 300:
                        break
                    continue
                idle = 0
                for src in readable:
                    try:
                        data = src.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        data = b""
                    if not data:
                        return
                    try:
                        peers[src].sendall(data)
                    except OSError:
                        return
        except OSError as exc:
            log(f"WebSocket 转发异常 {target}: {exc}")
        finally:
            try:
                up.close()
            except OSError:
                pass

    def _send_raw(self, status_line: str, headers: list[tuple[str, str]],
                  payload: bytes) -> None:
        out = [status_line]
        for key, value in headers:
            out.append(f"{key}: {value}")
        out.append(f"Content-Length: {len(payload)}")
        out.append("Connection: close")
        head = ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")
        try:
            self.request.sendall(head)
            if payload:
                self.request.sendall(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _rewrite_set_cookie(self, headers: list[tuple[str, str]]) -> None:
        """把上游 Set-Cookie 的 Path=/ 收窄到应用挂载目录，避免污染 NAS 根路径。"""
        prefix = self.server.prefix
        for i, (name, value) in enumerate(headers):
            if name.lower() != "set-cookie":
                continue
            parts = [p.strip() for p in value.split(";")]
            out = []
            for p in parts:
                if p.lower().startswith("path="):
                    target = p.split("=", 1)[1].strip()
                    if target in ("", "/"):
                        p = f"Path={prefix or '/'}"
                out.append(p)
            headers[i] = (name, "; ".join(out))

    def _error(self, code: int, reason: str) -> None:
        body = f"{code} {reason}".encode()
        self._send_raw(f"HTTP/1.1 {code} {reason}",
                       [("Content-Type", "text/plain; charset=utf-8")], body)

    # ── 转发 ──────────────────────────────────────────────────────────
    def _rewrite_location(self, headers: list[tuple[str, str]]) -> None:
        """重写上游 3xx 的 Location 头，把相对路径挂到网关前缀下。

        这是桌面内嵌能否工作的关键：应用挂在 /app/hermes-desktop 子路径，
        而上游返回的 `location: /login?next=%2F` 是站点根相对路径。
        浏览器会把它解析成 http://<host>:5666/login —— 那是 fnOS 自己的
        登录页，而不是应用的登录页，表现为「一点桌面图标就跳到飞牛登录页」。

        同时剥掉 Location 里的 token 参数：它是上游自己的一次性凭证，
        不应泄漏到地址栏。
        """
        prefix = self.server.prefix
        if not prefix:
            return
        for i, (name, value) in enumerate(headers):
            if name.lower() != "location":
                continue
            loc = value.strip()
            if not loc:
                continue

            # 拆出 query 与 fragment
            base, sep, query = loc.partition("?")
            frag = ""
            if "#" in base:
                base, _, frag = base.partition("#")
                frag = "#" + frag

            # 站点根相对路径 → 加上网关前缀
            if base.startswith("/") and not base.startswith("//"):
                base = prefix + base

            # 去掉上游的一次性 token 参数
            if query:
                params = [p for p in query.split("&")
                          if p and not p.lower().startswith("token=")]
                query = "&".join(params)

            rebuilt = base + (("?" + query) if query else "") + frag
            headers[i] = (name, rebuilt)

    def _forward(self, method: str, path: str, version: str,
                 headers: dict[str, str], body: bytes) -> None:
        target = self._strip_prefix(path)

        # WebSocket 升级请求必须走裸 socket 双向转发。
        # http.client 只会读一个完整的 HTTP 响应就结束，握手成功后
        # 的双向帧流它扛不住 —— 表现为前端报
        #   Desktop IPC bridge is unavailable.
        # 因为 Hermes 的「IPC 桥」就是这条 WS 连接。
        upgrade = ""
        for key, value in headers.items():
            if key.lower() == "upgrade":
                upgrade = value.strip().lower()
                break
        if upgrade == "websocket":
            self._proxy_websocket(target, headers, body)
            return

        hop = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length"}
        out_headers = {}
        for key, value in headers.items():
            if key.lower() not in hop:
                out_headers[key] = value
        upstream_origin = (
            f"http://{self.server.upstream_host}:{self.server.upstream_port}")
        out_headers["Host"] = (
            f"{self.server.upstream_host}:{self.server.upstream_port}")
        out_headers["X-Forwarded-For"] = "127.0.0.1"
        out_headers["X-Forwarded-Proto"] = "http"
        out_headers["X-Forwarded-Host"] = headers.get("Host", "localhost")

        # Hermes 校验 Origin 必须是「它自己」：POST /webapp/window-session
        # 否则 403 {"detail":"Webapp window handoff requires a same-origin
        # request"}。经本层访问时浏览器给的 Origin 是 http://<nas>:<port>，
        # 与上游自我认知 http://127.0.0.1:9120 不同，会被拒 —— 改写成上游地址。
        for key in list(out_headers):
            if key.lower() == "origin":
                out_headers[key] = upstream_origin
            elif key.lower() == "referer":
                value = out_headers[key]
                tail = value.split("://", 1)[-1]
                slash = tail.find("/")
                rest = tail[slash:] if slash >= 0 else "/"
                out_headers[key] = f"{upstream_origin}{rest}"
        if body:
            out_headers["Content-Length"] = str(len(body))

        conn = http.client.HTTPConnection(
            self.server.upstream_host, self.server.upstream_port, timeout=300)
        try:
            conn.request(method, target, body=body or None, headers=out_headers)
            resp = conn.getresponse()
            payload = resp.read()
            status_line = f"HTTP/1.1 {resp.status} {resp.reason}"
            resp_headers = [(k, v) for k, v in resp.getheaders()
                            if k.lower() not in ("transfer-encoding", "content-length",
                                                "connection", "keep-alive")]
            self._rewrite_set_cookie(resp_headers)
            self._rewrite_location(resp_headers)

            # 配对成功即落标记 cookie，首页不再被导向一次性链接
            if "window-session" in target and resp.status == 200:
                resp_headers.append((
                    "Set-Cookie",
                    f"{PAIRED_COOKIE}=1; Path={self.server.prefix or '/'}/; "
                    f"SameSite=Lax; Max-Age=86400"))

            # HTML/CSS 内容改写：补上网关前缀，让子路径下的资源能正确加载
            ctype = resp.getheader("Content-Type", "")
            kind = should_rewrite(target, ctype)
            if not kind and ("javascript" in ctype.lower()
                             or target.endswith(".js") or target.endswith(".mjs")):
                kind = "js"
            if kind and payload:
                if kind == "html":
                    payload = rewrite_html(payload, self.server.prefix)
                elif kind == "css":
                    payload = rewrite_css(payload, self.server.prefix)
                else:
                    payload = rewrite_javascript(payload, target, self.server.prefix)
                # 长度变了，重算 Content-Length
                resp_headers = [(k, v) for k, v in resp_headers
                                if k.lower() != "content-length"]

            self._send_raw(status_line, resp_headers, payload)
        except Exception as exc:
            log(f"上游请求失败 {method} {target}: {exc}")
            self._error(502, "Bad Gateway")
        finally:
            conn.close()

    def handle(self) -> None:
        try:
            parsed = self._read_request()
            if parsed is None:
                return
            method, path, version, headers, body = parsed

            # 入口改写：Hermes 的 Webapp 需要一次性启动链接完成配对，
            # 直接访问 / 会看到「Run hermes webapp on this computer...」。
            if self._maybe_redirect(path):
                return

            # WebSocket 升级：Hermes 的实时通道依赖它，需原样透传。
            if (headers.get("Upgrade", "").lower() == "websocket"
                    or "upgrade" in headers.get("Connection", "").lower()):
                self._handle_upgrade(path, headers)
                return

            self._forward(method, path, version, headers, body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover
            log(f"处理请求出错：{exc}")
            try:
                self._error(500, "Internal Server Error")
            except Exception:
                pass

    def _maybe_redirect(self, path: str) -> bool:
        """把首页请求改写成一次性启动链接，返回 True 表示已响应。

        Hermes 的 Webapp 用一次性启动链接完成配对：浏览器先打开
        /webapp/launch?token=... ，该页面把凭据写进 sessionStorage，
        之后才能正常使用。直接访问 / 会看到
        「Run hermes webapp on this computer to sign this browser in.」。

        控制 socket 只接受同 OS 用户，所以由本层（与应用同用户）代取链接，
        再用相对路径重定向浏览器过去，保持同源。
        """
        if not getattr(self.server, "launch_redirect", False):
            return False

        clean = path.split("?")[0]
        prefix = getattr(self.server, "prefix", "") or ""
        stripped = clean[len(prefix):] if prefix and clean.startswith(prefix) else clean
        if stripped != "/":
            return False

        # 已配对（带 cookie）就正常放行，避免 launch 页面
        # location.replace('/') 回来时又被导向一次性链接
        cookie = ""
        try:
            cookie = self.headers.get("Cookie", "") or ""
        except AttributeError:
            cookie = ""
        if PAIRED_COOKIE in cookie:
            return False

        home = getattr(self.server, "hermes_home", "") or ""
        launch = _launch_path(home)
        # 空 = 需要登录（sign_in），交给上游给登录页；
        # 根路径 = 没有可用的一次性链接，避免 302 到自己造成死循环。
        if not launch or launch.rstrip("/") in ("", "/"):
            return False

        target = f"{prefix}{launch}"
        self._send_raw("HTTP/1.1 302 Found", [
            ("Location", target),
            ("Content-Length", "0"),
            ("Cache-Control", "no-store"),
        ], b"")
        log(f"首页请求改写到一次性启动链接：{target}")
        return True

    def _handle_upgrade(self, path: str, headers: dict[str, str]) -> None:
        """WebSocket 升级：建立到上游的 TCP 连接后双向转发裸字节。"""
        import select

        target = self._strip_prefix(path)
        upstream_sock = socket.create_connection(
            (self.server.upstream_host, self.server.upstream_port), timeout=30)
        try:
            key = headers.get("Sec-WebSocket-Key", "")
            lines = [
                f"GET {target} HTTP/1.1",
                f"Host: {self.server.upstream_host}:{self.server.upstream_port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
            ]
            if key:
                lines.append(f"Sec-WebSocket-Key: {key}")
            for header in ("Sec-WebSocket-Version", "Sec-WebSocket-Protocol",
                           "Origin", "Cookie", "Authorization",
                           "X-Requested-With"):
                value = headers.get(header)
                if value:
                    lines.append(f"{header}: {value}")
            upstream_sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

            socks = [self.request, upstream_sock]
            while True:
                ready, _, _ = select.select(socks, [], [], 60)
                if not ready:
                    break
                for s in ready:
                    other = upstream_sock if s is self.request else self.request
                    try:
                        data = s.recv(RECV_BUF)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        finally:
            try:
                upstream_sock.close()
            except OSError:
                pass


class ProxyUnixServer(ThreadingUnixHTTPServer):
    prefix: str
    upstream_host: str
    upstream_port: int
    socket_path: str
    hermes_home: str = ""
    launch_redirect: bool = False


def serve(socket_path: str, upstream: tuple[str, int], prefix: str,
          hermes_home: str = "", launch_redirect: bool = False) -> int:
    server = ProxyUnixServer(socket_path, ProxyHandler, prefix, upstream)
    server.hermes_home = hermes_home
    server.launch_redirect = launch_redirect
    log(f"监听 {socket_path}")
    log(f"上游 {upstream[0]}:{upstream[1]}  前缀 {prefix or '(无)'}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("收到中断，准备退出")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True, help="Unix Socket 路径")
    ap.add_argument("--upstream", required=True, help="上游 host:port")
    ap.add_argument("--prefix", default="", help="要剥离的路径前缀")
    ap.add_argument("--hermes-home", default="",
                    help="Hermes 的 home 目录（含 gateway.sock），用于申请一次性启动链接")
    ap.add_argument("--enable-launch-redirect", action="store_true",
                    help="启用首页→一次性启动链接的重定向（仅在 webapp 需要登录时才有用；"
                         "绑回环、免登录时上游直接返回主界面，不该再重定向）")
    ap.add_argument("--enable-autostart", action="store_true",
                    help="允许适配层在上游未就绪时拉起它（默认关闭，"
                         "启动由 cmd/main 负责，避免与 fnOS 抢 host）")
    ap.add_argument("--autostart", default="",
                    help="上游未就绪时用这条命令拉起它（如 hermes gateway run）")
    ap.add_argument("--autostart-cwd", default="",
                    help="拉起上游时的工作目录")
    ap.add_argument("--tcp-listen", default="",
                    help="额外的对外 TCP 监听地址，如 0.0.0.0:9121。"
                         "用于在浏览器里直接访问（免登录），"
                         "Host 头保持回环地址，上游仍判定为本地请求")
    args = ap.parse_args()

    host, _, port = args.upstream.rpartition(":")
    if not host or not port.isdigit():
        die = f"上游格式错误：{args.upstream}（应为 host:port）"
        print(f"[gateway-proxy] {die}", file=sys.stderr)
        return 1

    # autostart 默认关闭。
    # 它会在「上游暂时没监听」时自己拉一个 Hermes —— 但 fnOS 启动应用时
    # 本来就在启动 Hermes，两者会抢同一个 host 与 profile 锁，于是
    # 新实例被拒（"A gateway already owns this host..."），表现为
    # 应用「无法启用」。启动由 cmd/main 负责，适配层不该越权。
    if args.autostart and args.enable_autostart:
        _ensure_upstream(args.autostart, args.autostart_cwd,
                         host, int(port))
    elif args.autostart:
        log("已忽略 autostart（启动由 cmd/main 负责，避免与 fnOS 抢 host）")

    tcp_thread = None
    if args.tcp_listen:
        tcp_host, _, tcp_port = args.tcp_listen.rpartition(":")
        if not tcp_host or not tcp_port.isdigit():
            print(f"[gateway-proxy] 无效的 --tcp-listen: {args.tcp_listen}", file=sys.stderr)
            return 1
        # 强制只绑回环：对外访问统一走 fnOS 网关（它会校验飞牛登录态）。
        # 若对 0.0.0.0 开放，局域网内任何人都能直接操作 Hermes，
        # 绕过网关的鉴权 —— 而 Hermes 绑回环后自身是免登录的。
        if tcp_host not in ("127.0.0.1", "localhost", "::1"):
            log(f"TCP 通道改为只监听回环（原 {tcp_host}）："
                f"对外访问请走 fnOS 网关 /app/hermes-desktop")
            tcp_host = "127.0.0.1"
        tcp_thread = threading.Thread(
            target=_serve_tcp,
            args=(tcp_host, int(tcp_port), (host, int(port)), args.prefix,
                  args.hermes_home, args.enable_launch_redirect),
            daemon=True)
        tcp_thread.start()
        log(f"额外 TCP 监听 {tcp_host}:{tcp_port}（浏览器直连，免登录）")

    try:
        return serve(args.socket, (host, int(port)), args.prefix,
                     args.hermes_home, args.enable_launch_redirect)
    finally:
        pass


def _ensure_upstream(command: str, cwd: str, host: str, port: int) -> None:
    """上游没在监听时，用给定命令把它拉起来。

    场景：fnOS 里应用被停止后，网关适配层还活着，但连不上上游，
    于是桌面图标点开报「拒绝连接」。这里让适配层充当守护，
    自己把上游拉起来（适配层与应用同属一个用户，有执行权限）。
    """
    import subprocess
    import time

    def _up_alive() -> bool:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False

    if _up_alive():
        return

    log(f"上游 {host}:{port} 未就绪，尝试拉起：{command}")
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd or None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        log(f"已拉起上游（PID={proc.pid}）")
    except Exception as exc:
        log(f"拉起上游失败：{exc}")
        return

    for _ in range(30):
        time.sleep(2)
        if _up_alive():
            log("上游已就绪")
            return
    log("等待上游超时")


#: 一次性启动链接的缓存：(path, 取到的时间)。Hermes 的 launch 链接是
#: 一次性且互斥的 —— 每次申请都会让上一个失效。浏览器打开首页时往往伴
#: 随 favicon 等并发请求，若每次都申请新的，浏览器手里那个立刻作废，
#: 于是看到 "This launch link was already used or has expired"。
#: 所以短时间内的请求必须复用同一个链接。
#: 配对标记 cookie。launch 成功后由本层设置，用来区分
#: 「首次访问（需要走一次性链接）」和「已配对（正常访问）」——
#: 否则 launch 页面 location.replace('/') 回到首页时，
#: 本层又会把它导向 launch，而那个链接只能用一次 → 报「已用过」。
PAIRED_COOKIE = "hermes_fnos_paired"

_LAUNCH_CACHE: dict[str, tuple[str, float]] = {}
_LAUNCH_TTL = 90.0


def _launch_path(hermes_home: str) -> str:
    """向 gateway 控制 socket 申请一次性启动链接，返回其 path+query。

    Hermes 的 Webapp 用「一次性启动链接」完成配对：
      hermes webapp → 通过控制 socket 取 link → 浏览器打开 link
      → /webapp/launch 页面把凭据写进 sessionStorage → 之后正常使用
    直接访问 / 会看到「Run hermes webapp on this computer to sign this
    browser in.」，因为那个标签页还没有凭据。

    控制 socket 只接受同 OS 用户（SO_PEERCRED），而适配层与应用同用户，
    因此由适配层代取。注意返回的 url 指向 127.0.0.1:9120，浏览器访问不到，
    这里只取它的 path+query，由本层用相对路径重定向回去，保持同源。
    """
    import time

    if not hermes_home:
        # 没显式指定时，从脚本自身位置推断：适配层被部署在应用数据目录，
        # 那里就有 gateway.sock。这样即便旧版 cmd/main 没传 --hermes-home
        # （cmd/ 目录是 root 只读，升级前无法替换），也能正常工作。
        hermes_home = os.path.dirname(os.path.abspath(__file__))

    # 命中缓存且未过期就直接复用
    cached = _LAUNCH_CACHE.get(hermes_home)
    if cached and (time.monotonic() - cached[1]) < _LAUNCH_TTL:
        return cached[0]
    sock_path = os.path.join(hermes_home, "gateway.sock")
    if not os.path.exists(sock_path):
        return ""
    try:
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.settimeout(5)
        peer.connect(sock_path)
        peer.sendall(json.dumps({
            "protocol": 1, "id": 1, "verb": "webapp-launch", "params": {},
        }).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = peer.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        peer.close()
        resp = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8", "replace"))
        if not resp.get("ok"):
            return ""
        result = resp.get("result") or {}
        # sign_in=True：Webapp 处于「需要登录」状态（配了 dashboard.basic_auth
        # 就会这样），上游给的是一个「去登录」的根地址，而不是一次性链接。
        # 此时绝不能拿它做重定向 —— 否则 302 到应用自己的根路径，浏览器
        # 无限重定向，iframe 里表现就是空白/转圈。
        # 返回空字符串表示「本层不插手」，让上游自己 302 到 /login。
        if result.get("sign_in"):
            return ""
        url = result.get("url") or ""
        if not url:
            return ""
        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        # 凭据在 launch URL 的 fragment 里（#ticket=xxx），launch 页面用
        #   new URLSearchParams(location.hash.slice(1))
        # 读取。但浏览器跟随 302 重定向时会**丢弃** Location 里的 fragment
        # （实测：重定向后地址栏里没有 #ticket，上游于是报
        #  "This launch link was already used or has expired"）。
        #
        # 所以这里把 fragment 的键值对改写成 query —— query 在重定向中
        # 会被保留下来；再由 rewrite_html 把页面的读取位置从
        # location.hash 改成 location.search。
        if parts.fragment:
            frag = parts.fragment
            if "=" in frag:
                path = f"{path}?{frag}"
            else:
                path = f"{path}#{frag}"

        _LAUNCH_CACHE[hermes_home] = (path, time.monotonic())
        return path
    except Exception as exc:
        log(f"申请启动链接失败：{exc}")
        return ""


#: 代理持有的 socket fd（重载前需关闭，否则新进程 bind 同路径会失败）
_proxy_fds: set = set()


def _socket_arg() -> str:
    """从命令行里取出 --socket 的值。"""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--socket" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--socket="):
            return a.split("=", 1)[1]
    return ""


def _rewrite_response_body(raw: bytes, path: str, prefix: str) -> bytes:
    """对上游响应做与 socket 通道一致的改写（HTML 前缀、launch 凭据读取位置）。

    socket 通道走 ProxyHandler，响应经过 rewrite_html / rewrite_javascript；
    TCP 通道是原始字节转发，若不改写，launch 页面仍会从 location.hash 取
    凭据 —— 而浏览器重定向时已把 fragment 丢掉，于是配对失败。
    """
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return raw

    try:
        lines = head.decode("latin-1").split("\r\n")
    except Exception:
        return raw

    status = lines[0] if lines else ""
    headers = []
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers.append([k.strip(), v.strip()])

    ctype = ""
    for k, v in headers:
        if k.lower() == "content-type":
            ctype = v
            break

    if should_rewrite(path, ctype) == "html":
        body = rewrite_html(body, prefix)

    # launch 页面的 POST /webapp/window-session 成功即表示配对完成，
    # 在这里落一个标记 cookie，首页就不会再被导向一次性链接。
    paired = False
    if "window-session" in path and status.split()[1:2] == ["200"]:
        paired = True

    out_headers = []
    for k, v in headers:
        if k.lower() in ("content-length", "transfer-encoding", "connection"):
            continue
        out_headers.append((k, v))
    out_headers.append(("Content-Length", str(len(body))))
    out_headers.append(("Connection", "close"))
    if paired:
        out_headers.append((
            "Set-Cookie",
            f"{PAIRED_COOKIE}=1; Path={prefix or '/'}/; "
            f"SameSite=Lax; Max-Age=86400"))

    head_bytes = status.encode("latin-1") + b"\r\n"
    head_bytes += b"".join(f"{k}: {v}\r\n".encode("latin-1") for k, v in out_headers)
    head_bytes += b"\r\n"
    return head_bytes + body


def _serve_tcp(host: str, port: int, upstream: tuple[str, int], prefix: str,
               hermes_home: str = "", launch_redirect: bool = False) -> None:
    """在 TCP 上提供同样的 HTTP 转发（供浏览器直接访问）。

    与 Unix Socket 通道的差别只有一处：Host 头固定为回环地址，
    上游因此判定为本地请求 —— 不需要登录。
    """
    import socketserver

    up_host, up_port = upstream

    class TcpServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class Direct(http.server.BaseHTTPRequestHandler):
        """把 TCP 上收到的请求原样转给上游的 TCP 端口。

        用 BaseHTTPRequestHandler 而不是 StreamRequestHandler：
        后者没有 self.headers（请求头未解析），拿不到 Content-Length。
        """

        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # 不重写 handle_one_request —— 那会跳过 parse_request，
        # 导致 self.path / self.command / self.headers 都没有被解析。
        # 由标准流程解析完请求头后再交给 _relay。
        def do_GET(self):
            self._relay()

        def do_POST(self):
            self._relay()

        def do_PUT(self):
            self._relay()

        def do_DELETE(self):
            self._relay()

        def do_PATCH(self):
            self._relay()

        def do_HEAD(self):
            self._relay()

        def do_OPTIONS(self):
            self._relay()

        def _relay(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                query = ("?" + self.path.split("?")[1]) if "?" in self.path else ""
                stripped = path[len(prefix):] if prefix and path.startswith(prefix) else path
                if not stripped.startswith("/"):
                    stripped = "/" + stripped

                # 与 socket 通道一致：首页请求改写成一次性启动链接
                # 已配对（带 cookie）就直接放行，避免死循环
                if (launch_redirect and stripped == "/"
                        and PAIRED_COOKIE not in (
                            self.headers.get("Cookie") or "")):
                    link = _launch_path(hermes_home)
                    if link and link.rstrip("/") not in ("", "/"):
                        target = f"{prefix}{link}"
                        self.send_response(302)
                        self.send_header("Location", target)
                        self.send_header("Content-Length", "0")
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        log(f"首页请求改写到一次性启动链接：{target}")
                        return

                up = socket.create_connection((up_host, up_port), timeout=60)
                lines = [f"{self.command} {stripped}{query} HTTP/1.1",
                         f"Host: {up_host}:{up_port}"]
                for k, v in self.headers.items():
                    low = k.lower()
                    if low in ("host", "content-length", "connection"):
                        continue
                    # Hermes 会校验 Origin 必须是「它自己」：
                    #   POST /webapp/window-session
                    #   → 403 {"detail":"Webapp window handoff requires
                    #           a same-origin request"}
                    # 浏览器经本层访问时 Origin 是 http://<nas>:<port>，
                    # 与上游自我认知 http://127.0.0.1:9120 不同，会被拒。
                    # 于是把 Origin / Referer 改写成上游自身地址。
                    if low == "origin":
                        lines.append(f"Origin: http://{up_host}:{up_port}")
                        continue
                    if low == "referer":
                        # 保留路径部分，只换掉 scheme://host:port
                        tail = v.split("://", 1)[-1]
                        slash = tail.find("/")
                        rest = tail[slash:] if slash >= 0 else "/"
                        lines.append(
                            f"Referer: http://{up_host}:{up_port}{rest}")
                        continue
                    lines.append(f"{k}: {v}")
                if body:
                    lines.append(f"Content-Length: {len(body)}")
                lines.append("Connection: close")
                up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                if body:
                    up.sendall(body)

                # 读完上游响应：Connection: close，上游会自己收尾。
                raw = b""
                while True:
                    d = up.recv(65536)
                    if not d:
                        break
                    raw += d
                up.close()

                raw = _rewrite_response_body(raw, stripped, prefix)
                self.wfile.write(raw)
                self.wfile.flush()
            except Exception as exc:
                log(f"TCP 通道请求出错 {self.path}: {exc}")
            finally:
                self.close_connection = True

    log(f"TCP 直连通道就绪 {host}:{port} → {up_host}:{up_port}")
    try:
        tcp_server = TcpServer((host, port), Direct)
        tcp_server.serve_forever()
    except Exception as exc:
        log(f"TCP 监听失败 {host}:{port} → {exc}")


if __name__ == "__main__":
    sys.exit(main())
