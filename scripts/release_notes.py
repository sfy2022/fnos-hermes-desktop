#!/usr/bin/env python3
"""生成 Release 说明：应用侧变更 + 上游 Hermes 变更 + 下载安装信息。

为什么需要这个
--------------
之前 Release 说明是 CI 里一段 heredoc，只有版本号和 commit —— 用户看到
「有新版本」却不知道更新了什么，只能盲猜要不要升级。

变更来源分两处，缺一不可：
* **应用侧**：本仓库自己的提交（新增插件、修 bug、文档）。用 ``git log``
  拿，最可靠，不依赖 API。
* **上游**：NousResearch/hermes-agent 在两个构建之间的提交。用 compare API，
  起点是**上一次 Release 的 build-info.json 里记录的 upstream_sha**。
  上游版本号长期不变（0.22.5），所以不能拿 tag 当基准。

任何一步失败都只降级、不中断发布 —— 说明写得糙一点也比发布失败好。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

UA = "fnos-hermes-release-notes/1.0"
MAX_COMMITS = 40


def _request(url: str, token: str, accept: str = "application/vnd.github+json") -> bytes:
    headers = {"User-Agent": UA, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def get_json(url: str, token: str) -> dict:
    try:
        return json.loads(_request(url, token).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"[notes] {url} -> HTTP {exc.code}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[notes] {url} -> {exc}", file=sys.stderr)
    return {}


def get_bytes(url: str, token: str) -> bytes:
    try:
        return _request(url, token, accept="application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        print(f"[notes] 下载 {url} 失败：{exc}", file=sys.stderr)
    return b""


def short(line: str, n: int = 110) -> str:
    line = line.strip()
    return line if len(line) <= n else line[: n - 1] + "…"


def local_commits(prev_tag: str) -> tuple[list[str], int, bool]:
    """本仓库自 prev_tag 以来的提交（第一行标题）。

    返回 ``(标题列表, 总数, 是否缺少对比基准)``。最后一项为 True 表示
    有 prev_tag 但本地取不到它（浅克隆没带 tag）—— 这种情况宁可留白，
    也不要拿「最近 N 条」冒充「本次变更」，那会把人误导得更惨。
    """
    def run(args: list[str]) -> str:
        try:
            return subprocess.run(
                args, capture_output=True, text=True, timeout=60, check=False
            ).stdout
        except Exception:  # noqa: BLE001
            return ""

    def titles(text: str, limit: int | None = None) -> list[str]:
        items = [short(x) for x in text.strip().splitlines() if x.strip()]
        return items[:limit] if limit else items

    # 没有上一版：首次发布，给最近几条当上下文
    if not prev_tag:
        items = titles(run(["git", "log", "--no-merges", "--pretty=%s", "-10"]))
        return items, len(items), False

    if not run(["git", "rev-parse", "--verify", prev_tag]).strip():
        return [], 0, True

    items = titles(run(["git", "log", "--no-merges", "--pretty=%s", f"{prev_tag}..HEAD"]))
    return items, len(items), False


def prev_release(api: str, repo: str, token: str) -> dict:
    """最近一次已发布的 Release（发布步骤里当前 tag 还没建，所以就是上一版）。"""
    data = get_json(f"{api}/repos/{repo}/releases?per_page=1", token)
    if isinstance(data, list) and data:
        return data[0]
    return {}


def upstream_sha_of(api: str, repo: str, release: dict, token: str) -> str:
    """从某个 Release 的 build-info.json 里读上次构建的上游 sha。"""
    for asset in release.get("assets") or []:
        if asset.get("name") != "build-info.json":
            continue
        raw = get_bytes(asset.get("url", ""), token)
        if not raw:
            return ""
        try:
            return json.loads(raw.decode("utf-8")).get("upstream_sha") or ""
        except Exception:  # noqa: BLE001
            return ""
    return ""


def upstream_commits(prev_sha: str, cur_sha: str, token: str) -> list[str]:
    if not prev_sha or not cur_sha or prev_sha == cur_sha:
        return []
    data = get_json(
        "https://api.github.com/repos/NousResearch/hermes-agent"
        f"/compare/{prev_sha}...{cur_sha}",
        token,
    )
    out = []
    for c in data.get("commits") or []:
        msg = (c.get("commit") or {}).get("message") or ""
        first = msg.split("\n")[0]
        if first:
            out.append(f"`{c.get('sha', '')[:8]}` {short(first)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--api", default="https://api.github.com")
    ap.add_argument("--token", default="")
    ap.add_argument("--version", required=True)
    ap.add_argument("--sha", required=True, help="本次构建的上游 commit")
    ap.add_argument("--asset", required=True, help="fpk 文件名")
    ap.add_argument("--size", default="")
    ap.add_argument("--sha256", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    api = args.api.rstrip("/")
    token = args.token

    prev = prev_release(api, args.repo, token)
    prev_tag = prev.get("tag_name") or ""
    prev_sha = upstream_sha_of(api, args.repo, prev, token) if prev else ""

    mine, mine_n, no_base = local_commits(prev_tag)
    theirs = upstream_commits(prev_sha, args.sha, token)

    lines: list[str] = ["Hermes 官方 Desktop 工作区的飞牛原生版。", ""]

    # ── 更新内容
    lines.append("## 更新内容")
    lines.append("")

    if mine:
        span = f"自 {prev_tag} 起 {mine_n} 个提交" if prev_tag else "最近提交"
        lines.append(f"**应用侧**（{span}）")
        lines.append("")
        for item in mine[:MAX_COMMITS]:
            lines.append(f"- {item}")
        if mine_n > MAX_COMMITS:
            lines.append(f"- …以及另外 {mine_n - MAX_COMMITS} 个提交")
        lines.append("")
    elif no_base:
        lines.append(f"**应用侧**：无法对比 {prev_tag} 的变更（构建时未取到历史，见仓库 CI 配置）。")
        lines.append("")
    elif prev_tag:
        lines.append("**应用侧**：本版没有代码改动，只是重新构建。")
        lines.append("")

    if theirs:
        lines.append(
            f"**上游 Hermes**（`{prev_sha}` → `{args.sha}`，{len(theirs)} 个提交）"
        )
        lines.append("")
        for item in theirs[:MAX_COMMITS]:
            lines.append(f"- {item}")
        if len(theirs) > MAX_COMMITS:
            lines.append(f"- …以及另外 {len(theirs) - MAX_COMMITS} 个提交")
        lines.append("")
    elif prev_sha:
        lines.append(f"**上游 Hermes**：无变化（仍为 `{args.sha}`）。")
        lines.append("")
    else:
        lines.append(f"**上游 Hermes**：`{args.sha}`（首次发布，无对比基准）。")
        lines.append("")

    # ── 下载与安装
    lines += [
        "---",
        "",
        "## 下载与安装",
        "",
        f"**下载**：[`{args.asset}`](https://github.com/{args.repo}/releases/download/"
        f"v{args.version}/{args.asset})"
        + (f"（{args.size}）" if args.size else ""),
        "",
        "**安装**：飞牛桌面 → 应用中心 → 手动安装 → 选择该 fpk",
        "（已装旧版的直接选它即可，会走升级流程，用户数据保留）",
        "",
        "## 校验",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 版本 | {args.version} |",
        f"| 上游 commit | `{args.sha}` |",
        f"| 架构 | x86_64 |",
    ]
    if args.sha256:
        lines.append(f"| SHA256 | `{args.sha256}` |")
    if prev_tag:
        lines.append(f"| 对比基准 | {prev_tag} |")
    lines.append("")

    text = "\n".join(lines)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"[notes] 已写入 {args.out}（应用侧 {mine_n} 条，上游 {len(theirs)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
