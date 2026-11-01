"""fnOS 授权 — dashboard 插件后端（自包含）

不再依赖外部守护进程：pty 登录会话、设备清单、二维码生成都在 gateway 进程里完成。
挂载点 ``/api/plugins/fnos-auth/``。

设计要点
--------
* 每台设备一个独立 ``trim-cli --profile dev-<id>``，互不串号（本机沿用默认 profile）。
* ``trim-cli login`` 强制要求 TTY，所以用 ``pty.openpty()`` 起子进程并关掉回显。
* 授权失败 / 取消 / 超时会让 CLI 清掉该 profile 的已有会话，因此发起前先快照、
  失败时还原（``cache/session-backup/``）。
* 授权码只在内存里流转一次：不落盘、不进日志、错误信息里也会被抹掉。
"""

from __future__ import annotations

import base64
import io
import json
import os
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import termios
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

HOME = os.environ.get("HERMES_HOME") or "/vol1/@appdata/hermes-desktop"
CLI = os.path.join(HOME, "skills/fnos-trim/scripts/trim-cli")
CACHE = os.path.join(HOME, "cache")
BACKUP_DIR = os.path.join(CACHE, "session-backup")
DEVICE_FILE = os.path.join(CACHE, "fnos-authd-devices.json")
CLI_HOME = os.path.expanduser("~/.config/trim-cli")
CODE_TTL = 300
URL_RE = re.compile(rb"https?://[^\s\x1b]+")
LOOPBACK = ("localhost", "127.0.0.1", "::1")

try:
    import segno  # type: ignore
except ImportError:  # pragma: no cover - 没装就没有二维码，其余功能照常
    segno = None

router = APIRouter()
_lock = threading.Lock()


# ------------------------------------------------------------------ 工具函数
def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _extract_json(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ 设备清单
def _default_devices() -> list[dict]:
    return [{
        "id": "local",
        "name": "本机 fnOS",
        "host": "localhost",
        "port": 5666,
        "scheme": "http",
        "tls_insecure": False,
        "builtin": True,
        "profile": "",
        "created_at": time.time(),
    }]


def load_devices() -> list[dict]:
    with _lock:
        try:
            with open(DEVICE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                for d in data:
                    if d.get("id") == "local" and "profile" not in d:
                        d["profile"] = ""
                return data
        except (OSError, json.JSONDecodeError):
            pass
        data = _default_devices()
        _save_locked(data)
        return data


def _save_locked(devices: list[dict]) -> None:
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = DEVICE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(devices, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, DEVICE_FILE)
        os.chmod(DEVICE_FILE, 0o600)
    except OSError:
        pass


def save_devices(devices: list[dict]) -> None:
    with _lock:
        _save_locked(devices)


def get_device(device_id: str) -> Optional[dict]:
    for d in load_devices():
        if d["id"] == device_id:
            return d
    return None


def normalize_target(raw: str, prefer_https: bool = False) -> Optional[tuple[str, int, str]]:
    s = (raw or "").strip()
    if not s:
        return None
    scheme = ""
    m = re.match(r"^(https?)://", s, re.I)
    if m:
        scheme = m.group(1).lower()
        s = s[m.end():]
    s = s.rstrip("/")
    port = None
    if s.startswith("[") and "]" in s:
        host_part, _, rest = s[1:].partition("]")
        s = host_part
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif s.count(":") == 1:
        host_part, port_part = s.split(":", 1)
        if port_part.isdigit():
            s, port = host_part, int(port_part)
    host = s.strip("[]")
    if not host:
        return None
    is_ip = True
    try:
        socket.inet_aton(host)
    except OSError:
        try:
            socket.inet_pton(socket.AF_INET6, host)
        except OSError:
            is_ip = False
    if not scheme:
        if host in LOOPBACK:
            scheme = "http"
        elif port is None:
            scheme = "https" if (prefer_https or not is_ip) else "http"
        elif port in (443, 5667, 8443):
            scheme = "https"
        else:
            scheme = "http"
    if port is None:
        if host in LOOPBACK:
            port = 5666
        elif scheme == "https":
            port = 443 if not is_ip else 5667
        else:
            port = 5666
    if scheme == "http" and port == 443:
        scheme = "https"
    return host, int(port), scheme


# ------------------------------------------------------------------ CLI 调用
def cli_base(dev: dict) -> list[str]:
    args = [CLI]
    prof = dev.get("profile", f"dev-{dev['id']}")
    if prof:
        args += ["--profile", prof]
    host = dev["host"]
    if host not in LOOPBACK or int(dev["port"]) != 5666 or dev.get("scheme") != "http":
        args += ["--host", host, "--port", str(int(dev["port"])), "--scheme", dev.get("scheme", "http")]
        if dev.get("scheme") == "http" and host not in LOOPBACK:
            args.append("--allow-insecure-http")
        if dev.get("tls_insecure") and dev.get("scheme") == "https":
            args.append("--tls-insecure")
    return args


def run_cli(dev: dict, rest: list[str], timeout: int = 12) -> tuple[int, str]:
    try:
        p = subprocess.run(cli_base(dev) + rest, capture_output=True, timeout=timeout, text=True)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 125, str(exc)


_status_cache: dict[str, dict] = {}


def device_status(dev: dict, refresh: bool = False) -> dict:
    did = dev["id"]
    hit = _status_cache.get(did)
    if hit and not refresh and time.time() - hit["at"] < 15:
        return hit["data"]

    out = {
        "id": did,
        "name": dev.get("name") or dev["host"],
        "host": dev["host"],
        "port": int(dev["port"]),
        "scheme": dev.get("scheme", "https"),
        "builtin": bool(dev.get("builtin")),
        "authorized": False,
    }
    rc, text = run_cli(dev, ["user", "info"], timeout=7)
    payload = _extract_json(text) if rc == 0 else None
    if payload and payload.get("result") == "succ":
        info = payload.get("userInfo", {})
        out.update(
            authorized=True,
            user=info.get("user"),
            admin=bool(info.get("admin")),
            uid=info.get("uid"),
            groups=info.get("groups", []),
        )
        rc2, text2 = run_cli(dev, ["system", "host-name"], timeout=6)
        if rc2 == 0:
            p2 = _extract_json(text2) or {}
            out["host_name"] = p2.get("hostName")
            out["version"] = p2.get("trimVersion")
    else:
        low = text.lower()
        if "login first" in low or "session is required" in low:
            out["reason"] = "未授权"
        elif "timed out" in low or "connection" in low or "refused" in low or rc == 124:
            out["reason"] = "连接失败"
        else:
            out["reason"] = (text.strip().splitlines()[-1][:160] if text.strip() else f"exit {rc}")

    _status_cache[did] = {"at": time.time(), "data": out}
    return out


# ------------------------------------------------------------------ 授权会话
class LoginSession:
    def __init__(self, dev: dict) -> None:
        self.dev = dev
        self.proc: Optional[subprocess.Popen] = None
        self.master: Optional[int] = None
        self.buf = bytearray()
        self.ready = threading.Event()
        self.started_at = 0.0
        self.public_url: Optional[str] = None
        self.backup: Optional[str] = None
        self.lock = threading.Lock()

    # ---- 会话快照 / 还原
    def _session_path(self) -> str:
        prof = self.dev.get("profile", f"dev-{self.dev['id']}")
        if not prof:
            return os.path.join(CLI_HOME, "secure", "active.enc")
        return os.path.join(CLI_HOME, "secure", "profiles", f"{prof}.enc")

    def _snapshot(self) -> None:
        self.backup = None
        src = self._session_path()
        try:
            if os.path.isfile(src):
                os.makedirs(BACKUP_DIR, exist_ok=True)
                dst = os.path.join(BACKUP_DIR, f"{self.dev['id']}.enc")
                shutil.copy2(src, dst)
                self.backup = dst
        except OSError:
            self.backup = None

    def _restore(self) -> None:
        if not self.backup:
            return
        src = self._session_path()
        try:
            if not os.path.exists(src):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                shutil.copy2(self.backup, src)
        except OSError:
            pass
        finally:
            self._drop_backup()

    def _drop_backup(self) -> None:
        if self.backup:
            try:
                os.remove(self.backup)
            except OSError:
                pass
            self.backup = None

    # ---- pty 读取
    def _pump(self) -> None:
        fd = self.master
        if fd is None:
            return
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not r:
                if self.proc and self.proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.buf.extend(chunk)
            if b"Authorization code:" in self.buf:
                self.ready.set()

    def start(self) -> str:
        if self.proc and self.proc.poll() is None and not self.stale():
            assert self.public_url
            return self.public_url
        self.buf.clear()
        self.ready.clear()
        self.public_url = None
        self.started_at = time.time()
        self._snapshot()

        master, slave = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO          # 授权码不回显，不进缓冲区
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
        except termios.error:
            pass
        self.master = master
        self.proc = subprocess.Popen(
            cli_base(self.dev) + ["login", "--no-open"],
            stdin=slave, stdout=slave, stderr=slave,
            bufsize=0, start_new_session=True,
        )
        os.close(slave)
        threading.Thread(target=self._pump, daemon=True).start()

        if not self.ready.wait(timeout=25):
            tail = bytes(self.buf).decode("utf-8", "replace").strip()[-200:]
            self.kill()
            raise RuntimeError("生成授权链接超时" + (f" · {tail}" if tail else ""))
        m = URL_RE.search(bytes(self.buf))
        if not m:
            self.kill()
            raise RuntimeError("未能解析出授权链接")
        url = m.group(0).decode(errors="replace").rstrip('"')
        if self.dev.get("host") in LOOPBACK:
            port = int(self.dev.get("port", 5666))
            for h in ("localhost", "127.0.0.1", "[::1]"):
                url = url.replace(f"://{h}:{port}", f"://{lan_ip()}:{port}")
        self.public_url = url
        return url

    def stale(self) -> bool:
        return time.time() - self.started_at > CODE_TTL

    def submit(self, code: str) -> dict:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError("没有等待中的授权会话，请重新点「授权」")
        if self.stale():
            self.kill()
            raise RuntimeError("授权码已过期（5 分钟），请重新点「授权」")
        with self.lock:
            if self.master is None:
                raise RuntimeError("授权会话已关闭，请重新点「授权」")
            try:
                os.write(self.master, code.strip().encode() + b"\n")
            except OSError as exc:
                raise RuntimeError(f"写入授权码失败：{exc}") from exc
            try:
                rc = self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.kill()
                raise RuntimeError("设备在 30 秒内没有响应")
        text = bytes(self.buf).decode("utf-8", "replace")
        payload = _extract_json(text)
        ok = bool(payload and payload.get("token_persisted"))
        if ok:
            self._drop_backup()
        self.kill(restore=not ok)
        if ok:
            return payload
        err = text.strip().splitlines()[-1] if text.strip() else f"exit {rc}"
        raise RuntimeError("授权失败：" + err.replace(code.strip(), "•••")[:180])

    def kill(self, restore: bool = True) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if restore:
            self._restore()
        else:
            self._drop_backup()


_sessions: dict[str, LoginSession] = {}
_sess_lock = threading.Lock()


def session_for(dev: dict) -> LoginSession:
    with _sess_lock:
        s = _sessions.get(dev["id"])
        if s is None or (s.proc is not None and s.proc.poll() is not None):
            s = LoginSession(dev)
            _sessions[dev["id"]] = s
        return s


def _reap_stale() -> None:
    with _sess_lock:
        items = list(_sessions.items())
    for _, s in items:
        try:
            if s.proc and s.proc.poll() is None and s.stale():
                s.kill()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ 二维码
def qr_data_uri(text: str) -> Optional[str]:
    if segno is None:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(
        buf, kind="svg", scale=8, border=3, dark="#000000", light="#ffffff",
        xmldecl=False, svgns=True, nl=False)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------ 请求模型
class AddDevice(BaseModel):
    target: str
    name: str = ""
    scheme: str = "auto"
    tls_insecure: bool = False


class CodeIn(BaseModel):
    code: str


# ------------------------------------------------------------------ 路由
@router.get("/devices")
def list_devices(refresh: int = 0):
    _reap_stale()
    devs = load_devices()
    return {"lan": lan_ip(), "devices": [device_status(d, bool(refresh)) for d in devs]}


@router.get("/devices/{device_id}")
def one_device(device_id: str):
    dev = get_device(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="设备不存在")
    return device_status(dev, True)


@router.post("/devices")
def add_device(body: AddDevice):
    tls = bool(body.tls_insecure)
    sel = (body.scheme or "auto").lower()
    parsed = normalize_target(body.target, prefer_https=(sel != "http" and tls))
    if not parsed:
        raise HTTPException(status_code=400, detail="地址格式不对")
    host, port, auto_scheme = parsed
    scheme = sel if sel in ("http", "https") else auto_scheme
    if tls and scheme == "http":
        scheme = "https"
        if port == 5666:
            port = 5667
    devs = load_devices()
    if any(d["host"] == host and int(d["port"]) == port for d in devs):
        raise HTTPException(status_code=400, detail=f"{host}:{port} 已在列表中")
    dev = {
        "id": secrets.token_hex(3),
        "name": (body.name or "").strip() or host,
        "host": host,
        "port": port,
        "scheme": scheme,
        "tls_insecure": tls,
        "created_at": time.time(),
    }
    devs.append(dev)
    save_devices(devs)
    return {"ok": True, "device": device_status(dev, True)}


@router.post("/devices/{device_id}/delete")
def delete_device(device_id: str):
    save_devices([d for d in load_devices() if d["id"] != device_id])
    with _sess_lock:
        s = _sessions.pop(device_id, None)
    if s:
        s.kill(restore=False)
    _status_cache.pop(device_id, None)
    return {"ok": True}


@router.post("/devices/{device_id}/start")
def start_auth(device_id: str):
    dev = get_device(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="设备不存在")
    try:
        url = session_for(dev).start()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"url": url, "qr": qr_data_uri(url), "ttl": CODE_TTL}


@router.post("/devices/{device_id}/code")
def submit_code(device_id: str, body: CodeIn):
    code = (body.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="授权码为空")
    dev = get_device(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="设备不存在")
    try:
        payload = session_for(dev).submit(code)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _status_cache.pop(device_id, None)
    return {
        "ok": True,
        "user": payload.get("username"),
        "admin": payload.get("admin"),
        "uid": payload.get("uid"),
    }


@router.post("/devices/{device_id}/logout")
def logout(device_id: str):
    dev = get_device(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="设备不存在")
    with _sess_lock:
        s = _sessions.pop(device_id, None)
    if s:
        s.kill(restore=False)
    rc, out = run_cli(dev, ["logout"], timeout=15)
    _status_cache.pop(device_id, None)
    return {"ok": rc == 0, "output": out.strip()[-200:]}


@router.get("/health")
def health():
    return {
        "ok": True,
        "cli": os.access(CLI, os.X_OK),
        "qr": segno is not None,
        "sessions": len(_sessions),
    }
