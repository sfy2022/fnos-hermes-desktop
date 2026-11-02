#!/usr/bin/env python3
"""
平台自适应的 Release 发布脚本。

Gitea 与 GitHub 的 Release API 路径不同，本脚本根据环境变量自动选择：

  GitHub : GITHUB_API_URL=https://api.github.com + GH_TOKEN
           → POST /repos/{owner}/{repo}/releases
           → POST /repos/{owner}/{repo}/releases/{id}/assets?name=

  Gitea : GITHUB_API_URL=https://<host>/api/v1 + GITEA_TOKEN
           → 同上路径，Gitea 兼容 GitHub 的 releases API

用法（在工作流中调用）：
    python3 scripts/publish_release.py \
        --repo "$OWNER/$REPO_NAME" \
        --tag "$TAG" \
        --title "..." \
        --notes-file release-notes.md \
        --assets dist/hermes-desktop.fpk dist/build-info.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import quote


def die(msg: str) -> None:
    print(f"[publish] ✗ {msg}", file=sys.stderr)
    sys.exit(1)


def api_base() -> str:
    base = os.environ.get("GITHUB_API_URL", "").rstrip("/")
    if base:
        return base
    server = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    if server:
        return f"{server}/api/v1"
    return "https://api.github.com"


def upload_base() -> str:
    """GitHub 的 Release 附件上传必须走 uploads.github.com。

    官方 REST 文档里 "Upload a release asset" 的 host 单独写成了
    uploads.github.com，用 api.github.com 传附件会被拒 —— 而创建 Release
    本身走 api.github.com 是正常的。这个不对称极容易踩：
    结果是「Release 建出来了，但 Assets 里什么都没有」。

    Gitea 没有独立的上传域，沿用 api base。
    """
    base = api_base()
    if base == "https://api.github.com":
        return "https://uploads.github.com"
    return base


def token() -> str:
    # Gitea 优先用 GITEA_TOKEN，GitHub 用 GH_TOKEN/GITHUB_TOKEN
    for name in ("GITEA_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def request(method: str, url: str, tok: str, data: bytes | None = None,
            content_type: str | None = None,
            timeout: int = 120) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    if tok:
        req.add_header("Authorization", f"token {tok}")
    if content_type:
        req.add_header("Content-Type", content_type)
    # urllib 默认按 chunked 发；GitHub 的上传端点对 chunked 支持不稳，
    # 显式给出长度更保险。
    if data is not None:
        req.add_header("Content-Length", str(len(data)))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        die(f"网络错误 {url}: {exc}")


def api_json(method: str, url: str, tok: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    status, body = request(method, url, tok, data, "application/json" if data else None)
    if status >= 400:
        print(f"[publish] API {status}: {body[:400].decode('utf-8', 'replace')}", file=sys.stderr)
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def upload_asset(base: str, repo: str, rel_id: int, tok: str, path: str) -> bool:
    """上传附件；若已存在同名资产则先删除再传（实现覆盖）。

    返回 True 表示确实传上去了。Release 建好了却没附件是最难排查的状态
    （页面有版本、但 Assets 里只有 Source code），所以失败必须让上层感知。

    用 raw body + application/octet-stream，不用手拼 multipart：
    GitHub 的上传端点对 multipart 的边界/头要求严格，手拼容易 400，
    而 raw 上传是官方 curl 示例的写法。
    """
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        content = fh.read()
    size_mb = len(content) / 1048576

    # 查同名资产
    ext = os.path.splitext(name)[1].lower()
    status, body = request("GET", f"{base}/repos/{repo}/releases/{rel_id}/assets", tok)
    if status < 400:
        try:
            for asset in json.loads(body.decode("utf-8")):
                aname = asset.get("name") or ""
                if not asset.get("id"):
                    continue
                # fpk 只保留最新一份：文件名里带 upstream commit，
                # 每次构建都不同，不清就会在同一个 Release 里越堆越多。
                stale = aname == name or (ext == ".fpk" and aname.lower().endswith(".fpk"))
                if stale:
                    request("DELETE",
                            f"{base}/repos/{repo}/releases/assets/{asset['id']}", tok)
                    print(f"[publish]   删除旧资产 {aname}")
        except Exception:
            pass

    # 附件必须走 uploads.github.com（见 upload_base 说明）
    ub = upload_base()
    url = f"{ub}/repos/{repo}/releases/{rel_id}/assets?name={quote(name)}"
    print(f"[publish]   上传 {name} ({size_mb:.1f} MB) → {ub}")
    status, resp = request("POST", url, tok, content,
                           "application/octet-stream", timeout=600)
    if status >= 400:
        print(f"[publish]   上传 {name} 失败 {status}: {resp[:500].decode('utf-8','replace')}",
              file=sys.stderr)
        return False

    # 回读校验：GitHub 返回的 asset 里应带 size
    try:
        info = json.loads(resp.decode("utf-8"))
        got = info.get("size")
        if isinstance(got, int) and got != len(content):
            print(f"[publish]   {name} 上传后大小不一致：local={len(content)} remote={got}",
                  file=sys.stderr)
            return False
        print(f"[publish]   上传 {name} ✓ ({got or len(content)} 字节)")
    except Exception:
        print(f"[publish]   上传 {name} ✓")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--notes-file", required=True)
    ap.add_argument("--assets", nargs="*", default=[])
    args = ap.parse_args()

    tok = token()
    if not tok:
        print("[publish] 缺少 token（GITEA_TOKEN / GH_TOKEN / GITHUB_TOKEN），跳过发布")
        return 0

    base = api_base()
    repo = args.repo
    notes = open(args.notes_file, encoding="utf-8").read()
    print(f"[publish] 平台 API: {base}")
    print(f"[publish] 仓库: {repo}  tag: {args.tag}")

    # tag 已存在就复用 release，否则创建
    rel = api_json("GET", f"{base}/repos/{repo}/releases/tags/{args.tag}", tok)
    rel_id = rel.get("id")

    if not rel_id:
        rel = api_json("POST", f"{base}/repos/{repo}/releases", tok, {
            "tag_name": args.tag,
            "name": args.title,
            "body": notes,
            "draft": False,
            "prerelease": False,
        })
        rel_id = rel.get("id")
        if not rel_id:
            die("Release 创建失败")
        print(f"[publish] 已创建 Release id={rel_id}")
    else:
        print(f"[publish] Release 已存在 id={rel_id}，更新附件")

    ok = True
    uploaded = 0
    for path in args.assets:
        if os.path.isfile(path):
            if upload_asset(base, repo, int(rel_id), tok, path):
                uploaded += 1
            else:
                ok = False
        else:
            print(f"[publish]   跳过（不存在）：{path}")

    if uploaded == 0:
        print("[publish] ✗ 没有任何附件上传成功", file=sys.stderr)
        return 1
    if not ok:
        print("[publish] ✗ 部分附件上传失败", file=sys.stderr)
        return 1

    print(f"[publish] ✓ 完成：{args.tag}（{uploaded} 个附件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
