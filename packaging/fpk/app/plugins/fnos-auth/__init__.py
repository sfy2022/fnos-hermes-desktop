"""fnOS 授权 — 原生插件（工具 + dashboard tab）

* 工具：``fnos_auth_*``，让 agent 直接列设备 / 加设备 / 发起授权 / 提交授权码 / 撤销。
* 界面：``dashboard/`` 提供左侧导航里的「fnOS 授权」tab。

两端共用同一份核心实现（``dashboard/plugin_api.py``），所以从工具发起的会话
也能在面板里提交授权码，反之亦然：会话表挂在 ``sys`` 上，跨模块实例共享。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
_CORE = None


def _core():
    """按需加载核心实现（dashboard/plugin_api.py），只加载一次。"""
    global _CORE
    if _CORE is not None:
        return _CORE
    for name, mod in list(sys.modules.items()):
        if name.endswith("fnos_auth_core_impl"):
            _CORE = mod
            return _CORE
    spec = importlib.util.spec_from_file_location(
        "fnos_auth_core_impl", _ROOT / "dashboard" / "plugin_api.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("找不到 dashboard/plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fnos_auth_core_impl"] = mod    # 让 dashboard 侧复用同一个实例
    spec.loader.exec_module(mod)
    _CORE = mod
    return mod


def _shared_sessions() -> dict:
    """会话表挂在 sys 上，避免同一进程里两处 import 各持一份。"""
    return sys.__dict__.setdefault("_hermes_fnos_auth_sessions", {})


def _shared_status() -> dict:
    return sys.__dict__.setdefault("_hermes_fnos_auth_status", {})


def _wire(core) -> None:
    """把核心模块的私有两张表指到进程级共享表上。"""
    core._sessions = _shared_sessions()
    core._status_cache = _shared_status()


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


# --------------------------------------------------------------------- 工具
def _h_devices(args: dict, **_kw) -> str:
    try:
        core = _core(); _wire(core)
        devices = core.list_devices(refresh=1 if args.get("refresh") else 0)
        rows = [{
            "id": d["id"],
            "name": d["name"],
            "address": f"{d['scheme']}://{d['host']}:{d['port']}",
            "authorized": d["authorized"],
            "user": d.get("user"),
            "admin": d.get("admin"),
            "state": "已授权" if d["authorized"] else d.get("reason", "未授权"),
        } for d in devices["devices"]]
        return _json({"ok": True, "lan": devices["lan"], "devices": rows})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _h_add(args: dict, **_kw) -> str:
    try:
        core = _core(); _wire(core)
        body = core.AddDevice(
            target=str(args.get("target", "")),
            name=str(args.get("name", "") or ""),
            scheme=str(args.get("scheme", "auto") or "auto"),
            tls_insecure=bool(args.get("tls_insecure")),
        )
        out = core.add_device(body)
        d = out["device"]
        return _json({
            "ok": True,
            "device": {
                "id": d["id"], "name": d["name"],
                "address": f"{d['scheme']}://{d['host']}:{d['port']}",
                "authorized": d["authorized"],
                "state": "已授权" if d["authorized"] else d.get("reason", "未授权"),
            },
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _h_start(args: dict, **_kw) -> str:
    try:
        core = _core(); _wire(core)
        out = core.start_auth(str(args.get("device_id", "")))
        return _json({
            "ok": True,
            "url": out["url"],
            "expires_in": out.get("ttl"),
            "next": "在浏览器打开这个链接，用 fnOS 账号登录并确认授权，"
                    "把页面给出的一次性 code 交给 fnos_auth_submit；"
                    "面板里也能看到同一会话的二维码。",
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _h_submit(args: dict, **_kw) -> str:
    try:
        core = _core(); _wire(core)
        out = core.submit_code(str(args.get("device_id", "")),
                               core.CodeIn(code=str(args.get("code", ""))))
        return _json({"ok": True, **out})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _h_check(args: dict, **_kw) -> str:
    try:
        core = _core(); _wire(core)
        d = core.one_device(str(args.get("device_id", "")))
        return _json({
            "ok": True,
            "device": {
                "id": d["id"], "name": d["name"],
                "address": f"{d['scheme']}://{d['host']}:{d['port']}",
                "authorized": d["authorized"],
                "user": d.get("user"),
                "state": "已授权" if d["authorized"] else d.get("reason", "未授权"),
            },
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _h_logout(args: dict, **_kw) -> str:
    try:
        core = _core(); _wire(core)
        return _json(core.logout(str(args.get("device_id", ""))))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def _schema(name: str, description: str, properties: dict, required=None) -> dict:
    params: dict = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


_S_ID = {"type": "string", "description": "设备 id（从 fnos_auth_devices 拿，本机是 local）"}

_TOOLS = (
    (
        "fnos_auth_devices",
        _schema(
            "fnos_auth_devices",
            "列出已登记的 fnOS 设备及其授权状态（是否已授权、账号、主机名、版本）。",
            {"refresh": {"type": "boolean", "description": "true 表示强制重新探测，忽略 15 秒缓存"}},
        ),
        _h_devices,
        "🔑",
    ),
    (
        "fnos_auth_add",
        _schema(
            "fnos_auth_add",
            "登记一台 fnOS 设备，用于后续 OAuth 授权。地址支持 IP、域名、带端口或显式协议。",
            {
                "target": {"type": "string", "description": "IP 或域名，例如 192.168.1.28、nas.example.com、192.168.1.2:5667"},
                "name": {"type": "string", "description": "备注名（可选）"},
                "scheme": {"type": "string", "enum": ["auto", "http", "https"], "description": "协议，默认 auto"},
                "tls_insecure": {"type": "boolean", "description": "自签证书时忽略校验（仅 https）"},
            },
            ["target"],
        ),
        _h_add,
        "➕",
    ),
    (
        "fnos_auth_start",
        _schema(
            "fnos_auth_start",
            "为某台设备发起 fnOS OAuth 授权，返回一次性授权链接（5 分钟有效）。"
            "用户在浏览器登录并确认授权后，把页面显示的 code 交给 fnos_auth_submit。",
            {"device_id": _S_ID},
            ["device_id"],
        ),
        _h_start,
        "🚀",
    ),
    (
        "fnos_auth_submit",
        _schema(
            "fnos_auth_submit",
            "提交 fnOS 授权页面给出的一次性授权码，完成令牌交换。",
            {"device_id": _S_ID, "code": {"type": "string", "description": "一次性授权码"}},
            ["device_id", "code"],
        ),
        _h_submit,
        "✅",
    ),
    (
        "fnos_auth_check",
        _schema(
            "fnos_auth_check",
            "重新探测一台设备的授权状态，可用来确认设备重启后令牌是否仍然有效。",
            {"device_id": _S_ID},
            ["device_id"],
        ),
        _h_check,
        "🔍",
    ),
    (
        "fnos_auth_logout",
        _schema(
            "fnos_auth_logout",
            "撤销某台设备保存在本机的授权（只清本机会话，不代表服务端令牌被撤销）。",
            {"device_id": _S_ID},
            ["device_id"],
        ),
        _h_logout,
        "🚪",
    ),
)


def register(ctx) -> None:
    """注册工具（由插件加载器调用一次）。"""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="fnos_auth",
            schema=schema,
            handler=handler,
            emoji=emoji,
            description=schema["description"],
        )
    log.debug("fnos-auth: registered %d tools", len(_TOOLS))
