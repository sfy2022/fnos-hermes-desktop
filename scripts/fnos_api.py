#!/usr/bin/env python3
"""
fnOS 开放 API 客户端（Python 标准库实现）

用途
----
飞牛 fnOS 向应用开放了一套系统 API，经 Unix Socket 暴露：
    Socket : /var/run/trim_open_gateway_apiscope.socket
    路径   : POST /api/v1/trimapp
    认证   : Authorization: Bearer <TRIM_API_TOKEN>
    载荷   : {"reqId": "...", "req": "<接口名>", "appName": "<应用名>", "data": {...}}

响应信封：{"code": <int>, "msg": "<str>", "data": <任意>}

与 trim-cli 的关系
----------------
* trim-cli   —— 走 fnOS 的 HTTP/CGI 接口，功能全（文件、Docker、存储、用户…），
                但需要 OAuth 授权，适合让 Agent 操作 NAS。
* 本客户端   —— 走 fnOS 开放 API 的应用通道，鉴权用 TRIM_API_TOKEN，
                不需要用户手动授权，适合应用自身的系统集成（如目录授权、主题）。

用法
----
    python3 scripts/fnos_api.py <req> [key=value ...]
    python3 scripts/fnos_api.py --list-requests

示例：
    python3 scripts/fnos_api.py system.info
    python3 scripts/fnos_api.py file.list path=/vol1/1000
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid

DEFAULT_SOCKET = "/var/run/trim_open_gateway_apiscope.socket"
DEFAULT_PATH = "/api/v1/trimapp"
RESPONSE_LIMIT = 8 * 1024 * 1024


def _post_unix(socket_path: str, http_path: str, payload: bytes,
               token: str, timeout: float = 15.0) -> tuple[int, bytes]:
    """在 Unix Socket 上发一个 HTTP POST，返回 (status, body)。"""
    request = (
        f"POST {http_path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Accept: application/json\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("utf-8") + payload

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        sock.sendall(request)
        chunks = []
        total = 0
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            total += len(data)
            if total > RESPONSE_LIMIT:
                break
        raw = b"".join(chunks)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    head, _, body = raw.partition(b"\r\n\r\n")
    status = 0
    first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = first.split(" ")
    if len(parts) > 1 and parts[1].isdigit():
        status = int(parts[1])
    return status, body


def call(req: str, data: dict | None = None, *, socket_path: str | None = None,
         http_path: str | None = None, token: str | None = None,
         app_name: str | None = None, timeout: float = 15.0) -> dict:
    """调用一次 fnOS 开放 API，返回解析后的响应信封。"""
    socket_path = socket_path or os.environ.get(
        "TRIM_API_SOCKET", DEFAULT_SOCKET)
    http_path = http_path or os.environ.get("TRIM_API_PATH", DEFAULT_PATH)
    token = token or os.environ.get("TRIM_API_TOKEN", "")
    app_name = app_name or os.environ.get("TRIM_APPNAME", "hermes-desktop")

    if not token.strip():
        return {"code": -1, "msg": "缺少 TRIM_API_TOKEN，无法调用 fnOS 开放 API", "data": None}
    if not os.path.exists(socket_path):
        return {"code": -1, "msg": f"fnOS API socket 不存在：{socket_path}", "data": None}

    payload = json.dumps({
        "reqId": f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        "req": req,
        "appName": app_name,
        "data": data or {},
    }, ensure_ascii=False).encode("utf-8")

    try:
        status, body = _post_unix(socket_path, http_path, payload, token, timeout)
    except socket.timeout:
        return {"code": -1, "msg": "fnOS API 请求超时", "data": None}
    except Exception as exc:
        return {"code": -1, "msg": f"fnOS API 调用失败：{exc}", "data": None}

    text = body.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except Exception:
        return {"code": status, "msg": text[:400], "data": None}


def _parse_kv(items: list[str]) -> dict:
    """把 ["a=1", "b=x"] 解析成 dict；值里的 = 用 split(...,1) 保留。"""
    out: dict = {}
    for item in items:
        if "=" in item:
            key, _, value = item.partition("=")
            out[key.strip()] = value.strip()
        else:
            out[item.strip()] = True
    return out


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--list-requests":
        print("用法：python3 fnos_api.py <req> [key=value ...]")
        print("环境变量：TRIM_API_TOKEN（必填）、TRIM_APPNAME（默认 hermes-desktop）")
        print("示例：python3 fnos_api.py system.info")
        return 0

    req = args[0]
    data = _parse_kv(args[1:])
    resp = call(req, data)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    code = resp.get("code")
    return 0 if code in (0, 200, None) else 1


if __name__ == "__main__":
    sys.exit(main())
