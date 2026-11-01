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


def token() -> str:
    # Gitea 优先用 GITEA_TOKEN，GitHub 用 GH_TOKEN/GITHUB_TOKEN
    for name in ("GITEA_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def request(method: str, url: str, tok: str, data: bytes | None = None,
            content_type: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    if tok:
        req.add_header("Authorization", f"token {tok}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
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


def upload_asset(base: str, repo: str, rel_id: int, tok: str, path: str) -> None:
    """上传附件；若已存在同名资产则先删除再传（实现覆盖）。"""
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        content = fh.read()

    # 查同名资产
    status, body = request("GET", f"{base}/repos/{repo}/releases/{rel_id}/assets", tok)
    if status < 400:
        try:
            for asset in json.loads(body.decode("utf-8")):
                if asset.get("name") == name and asset.get("id"):
                    request("DELETE",
                            f"{base}/repos/{repo}/releases/assets/{asset['id']}", tok)
                    print(f"[publish]   删除旧资产 {name}")
                    break
        except Exception:
            pass

    # multipart/form-data 上传
    boundary = "----fnoshermesdesktopboundary"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="attachment"; filename="{name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    url = f"{base}/repos/{repo}/releases/{rel_id}/assets?name={name}"
    status, resp = request("POST", url, tok, body,
                           f"multipart/form-data; boundary={boundary}")
    if status >= 400:
        print(f"[publish]   上传 {name} 失败 {status}: {resp[:200].decode('utf-8','replace')}",
              file=sys.stderr)
        return
    print(f"[publish]   上传 {name} ✓")


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

    for path in args.assets:
        if os.path.isfile(path):
            upload_asset(base, repo, int(rel_id), tok, path)
        else:
            print(f"[publish]   跳过（不存在）：{path}")

    print(f"[publish] ✓ 完成：{args.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
