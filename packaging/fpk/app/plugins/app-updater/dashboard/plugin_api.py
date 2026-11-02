"""应用更新 — 检查 GitHub Releases、下载 fpk、交给 trim-cli 安装。

挂载点 ``/api/plugins/app-updater/``

设计要点
--------
* 只做「检查 + 提示」，升级必须由用户点按钮触发 —— 安装会重启应用，
  正在跑的会话会断，不能静默做。
* 下载走 GitHub 官方地址，失败时依次换国内加速前缀；全失败才报错。
* 下载完用同 release 里的 ``build-info.json`` 校验 sha256，避免装到半截包。
* 安装交给 ``trim-cli app install-fpk --remote-path``，走 fnOS 原生升级流程，
  ``upgrade_callback`` 会重新拉上游源码并保留用户数据。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

REPO = "sfy2022/fnos-hermes-desktop"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
HOME = os.environ.get("HERMES_HOME") or "/vol1/@appdata/hermes-desktop"
CLI = os.path.join(HOME, "skills/fnos-trim/scripts/trim-cli")
CACHE = os.path.join(HOME, "cache", "update")
CLI_HOME = os.path.expanduser("~/.config/trim-cli")
UA = "hermes-desktop-fnos-updater/1.0"
TIMEOUT = 30
CHUNK = 256 * 1024
META_TTL = 600

# 顺序尝试：直连优先，之后是国内常见的 GitHub 加速前缀
MIRRORS = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
)

router = APIRouter()

_meta_cache: dict[str, Any] = {"at": 0.0, "data": None}
_lock = threading.Lock()
_job: dict[str, Any] = {
    "phase": "idle",      # idle | downloading | verifying | installing | done | error
    "percent": 0.0,
    "message": "",
    "file": "",
    "error": "",
    "via": "",
}


# ---------------------------------------------------------------- 工具函数
def _job_set(**kw: Any) -> None:
    with _lock:
        _job.update(kw)


def _job_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_job)


def _read_version() -> str:
    """当前安装版本：应用安装目录的 manifest 最权威。"""
    cands = [
        os.path.join(os.environ.get("TRIM_APPDEST", ""), "manifest"),
        "/var/apps/hermes-desktop/manifest",
        os.path.join(HOME, "app", "manifest"),
    ]
    for path in cands:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(r"\s*version\s*=\s*(\S+)", line)
                    if m:
                        return m.group(1).strip()
        except OSError:
            continue
    return "unknown"


def _vkey(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v)[:4]]
    return tuple(nums + [0] * (4 - len(nums))) if nums else (0, 0, 0, 0)


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_latest(force: bool = False) -> dict:
    """查最新 release，10 分钟内复用缓存（GitHub 匿名限流 60 次/小时）。"""
    now = time.time()
    if not force and _meta_cache["data"] and now - _meta_cache["at"] < META_TTL:
        return _meta_cache["data"]

    try:
        rel = _http_json(RELEASES_API)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise HTTPException(404, "仓库还没有发布任何 Release")
        if exc.code == 403:
            raise HTTPException(429, "GitHub API 限流，请稍后再试")
        raise HTTPException(502, f"查询 GitHub 失败：HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读错误
        raise HTTPException(502, f"连不上 GitHub：{exc}")

    assets = rel.get("assets") or []
    fpk = next((a for a in assets if a.get("name", "").endswith(".fpk")), None)
    meta = next((a for a in assets if a.get("name") == "build-info.json"), None)

    data = {
        "tag": rel.get("tag_name", ""),
        "version": (rel.get("tag_name") or "").lstrip("v"),
        "title": rel.get("name") or "",
        "notes": (rel.get("body") or "")[:8000],
        "published_at": rel.get("published_at") or "",
        "html_url": rel.get("html_url") or "",
        "asset": None,
        "meta_url": meta.get("browser_download_url") if meta else "",
    }
    if fpk:
        data["asset"] = {
            "name": fpk["name"],
            "size": fpk.get("size") or 0,
            "url": fpk.get("browser_download_url") or "",
        }
    _meta_cache.update(at=now, data=data)
    return data


def _authorized() -> bool:
    """trim-cli 是否已登录（安装 fpk 需要 session）。"""
    if not os.path.exists(CLI):
        return False
    for name in ("secure", "profiles"):
        if os.path.isdir(os.path.join(CLI_HOME, name)):
            return True
    return False


def _volume_id() -> int:
    """从数据目录路径推断卷号：/vol1/@appdata/... -> 1。"""
    m = re.match(r"/vol(\d+)/", HOME)
    return int(m.group(1)) if m else 1


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: str, size_hint: int = 0) -> str:
    """下载到 dest.part 再原子改名，边下边更新进度。"""
    _job_set(phase="downloading", percent=0.0, via=url.split("/")[2])
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or size_hint or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    if total:
                        _job_set(percent=round(done / total * 100, 1))
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _expected_sha(meta_url: str) -> str:
    """从 release 的 build-info.json 取 fpk 的 sha256。取不到就返回空串。"""
    if not meta_url:
        return ""
    for base in ("", *MIRRORS):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(base + meta_url, headers={"User-Agent": UA}),
                timeout=TIMEOUT,
            ) as resp:
                return json.loads(resp.read().decode("utf-8")).get("fpk_sha256") or ""
        except Exception:  # noqa: BLE001 - 只是个校验加成，失败不阻塞
            continue
    return ""


# ---------------------------------------------------------------- 路由
@router.get("/status")
def status(refresh: bool = False) -> dict:
    current = _read_version()
    out: dict[str, Any] = {
        "current": current,
        "job": _job_snapshot(),
        "authorized": _authorized(),
        "repo": REPO,
        "error": "",
    }
    try:
        latest = _fetch_latest(force=refresh)
    except HTTPException as exc:
        out["error"] = exc.detail
        return out

    out.update({
        "latest": latest["version"],
        "tag": latest["tag"],
        "title": latest["title"],
        "notes": latest["notes"],
        "published_at": latest["published_at"],
        "html_url": latest["html_url"],
        "asset": latest["asset"],
        "has_update": _vkey(latest["version"]) > _vkey(current),
    })
    return out


@router.post("/check")
def check() -> dict:
    return status(refresh=True)


@router.post("/download")
def download() -> dict:
    snap = _job_snapshot()
    if snap["phase"] in ("downloading", "verifying", "installing"):
        raise HTTPException(409, f"正在进行中：{snap['phase']}")

    latest = _fetch_latest()
    asset = latest.get("asset")
    if not asset:
        raise HTTPException(404, "最新 Release 里没有 fpk 附件")

    os.makedirs(CACHE, exist_ok=True)
    dest = os.path.join(CACHE, asset["name"])
    # 上一次已经下好且完整，直接复用
    if os.path.exists(dest) and asset["size"] and os.path.getsize(dest) == asset["size"]:
        _job_set(phase="done", percent=100.0, message="已存在完整安装包", file=dest)
        return {"file": dest, "cached": True}

    def worker() -> None:
        last_err = ""
        for base in ("", *MIRRORS):
            url = base + asset["url"]
            try:
                _download(url, dest, asset["size"])
                _job_set(phase="verifying", percent=100.0, message="校验完整性")
                want = _expected_sha(latest.get("meta_url", ""))
                if want:
                    got = _sha256(dest)
                    if got != want:
                        os.remove(dest)
                        raise ValueError(f"sha256 不匹配（期望 {want[:12]}…，实得 {got[:12]}…）")
                    _job_set(phase="done", message="下载完成并校验通过", file=dest)
                else:
                    _job_set(phase="done", message="下载完成（未取到校验值）", file=dest)
                return
            except Exception as exc:  # noqa: BLE001 - 换下一个源
                last_err = f"{url.split('/')[2]}：{exc}"
                continue
        _job_set(phase="error", error=f"所有源都失败（{last_err}）")

    _job_set(phase="downloading", percent=0.0, message="开始下载", file=dest,
             error="", via="")
    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "file": dest}


@router.get("/job")
def job() -> dict:
    return _job_snapshot()


@router.post("/install")
def install(file: str = "") -> dict:
    """调 trim-cli 安装。安装期间 fnOS 会重启应用，所以这里不等待完成。"""
    snap = _job_snapshot()
    if snap["phase"] == "installing":
        raise HTTPException(409, "已经有一轮安装在进行了")
    if not _authorized():
        raise HTTPException(403, "trim-cli 未授权，无法调用应用中心安装。请先在「fnOS 授权」里授权本机。")

    target = file or snap.get("file") or ""
    if not target or not os.path.isfile(target):
        raise HTTPException(404, "安装包不存在，请先下载")
    # 只允许装我们自己下到缓存目录里的包，杜绝任意路径
    if not os.path.realpath(target).startswith(os.path.realpath(CACHE) + os.sep):
        raise HTTPException(400, "非法的安装包路径")
    if not target.endswith(".fpk"):
        raise HTTPException(400, "不是 fpk 文件")

    cmd = [CLI, "--host", "localhost", "--port", "5666",
           "app", "install-fpk", "--remote-path", target,
           "--volume-id", str(_volume_id()), "--yes"]
    log_path = os.path.join(CACHE, "install.log")

    def worker() -> None:
        _job_set(phase="installing", message="已交给应用中心安装，应用即将重启")
        try:
            with open(log_path, "ab") as fh:
                subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=1800, check=False)
            _job_set(phase="done", message="安装命令已结束（若未重启请手动检查）")
        except Exception as exc:  # noqa: BLE001
            _job_set(phase="error", error=f"安装失败：{exc}")

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True, "cmd": " ".join(cmd[:6] + ["...", "--yes"])}


@router.post("/clean")
def clean() -> dict:
    """清掉缓存的安装包与日志。"""
    freed = 0
    if os.path.isdir(CACHE):
        for name in os.listdir(CACHE):
            path = os.path.join(CACHE, name)
            try:
                freed += os.path.getsize(path)
                os.remove(path) if os.path.isfile(path) else shutil.rmtree(path)
            except OSError:
                pass
    _job_set(phase="idle", percent=0.0, message="", file="", error="")
    return {"freed": freed}
