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

        hop = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length"}
        out_headers = {}
        for key, value in headers.items():
            if key.lower() not in hop:
                out_headers[key] = value
        out_headers["Host"] = f"{self.server.upstream_host}:{self.server.upstream_port}"
        out_headers["X-Forwarded-For"] = "127.0.0.1"
        out_headers["X-Forwarded-Proto"] = "http"
        out_headers["X-Forwarded-Host"] = headers.get("Host", "localhost")
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


def serve(socket_path: str, upstream: tuple[str, int], prefix: str) -> int:
    server = ProxyUnixServer(socket_path, ProxyHandler, prefix, upstream)
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

    if args.autostart:
        _ensure_upstream(args.autostart, args.autostart_cwd,
                         host, int(port))

    tcp_thread = None
    if args.tcp_listen:
        tcp_host, _, tcp_port = args.tcp_listen.rpartition(":")
        if not tcp_host or not tcp_port.isdigit():
            print(f"[gateway-proxy] 无效的 --tcp-listen: {args.tcp_listen}", file=sys.stderr)
            return 1
        tcp_thread = threading.Thread(
            target=_serve_tcp,
            args=(tcp_host, int(tcp_port), (host, int(port)), args.prefix),
            daemon=True)
        tcp_thread.start()
        log(f"额外 TCP 监听 {tcp_host}:{tcp_port}（浏览器直连，免登录）")

    try:
        return serve(args.socket, (host, int(port)), args.prefix)
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


def _serve_tcp(host: str, port: int, upstream: tuple[str, int], prefix: str) -> None:
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

                up = socket.create_connection((up_host, up_port), timeout=60)
                lines = [f"{self.command} {stripped}{query} HTTP/1.1",
                         f"Host: {up_host}:{up_port}"]
                for k, v in self.headers.items():
                    if k.lower() not in ("host", "content-length", "connection"):
                        lines.append(f"{k}: {v}")
                if body:
                    lines.append(f"Content-Length: {len(body)}")
                lines.append("Connection: close")
                up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                if body:
                    up.sendall(body)
                while True:
                    d = up.recv(65536)
                    if not d:
                        break
                    self.wfile.write(d)
                self.wfile.flush()
                up.close()
            except Exception as exc:
                log(f"TCP 通道请求出错 {self.path}: {exc}")
            finally:
                self.close_connection = True

    log(f"TCP 直连通道就绪 {host}:{port} → {up_host}:{up_port}")
    try:
        TcpServer((host, port), Direct).serve_forever()
    except Exception as exc:
        log(f"TCP 监听失败 {host}:{port} → {exc}")


if __name__ == "__main__":
    sys.exit(main())
