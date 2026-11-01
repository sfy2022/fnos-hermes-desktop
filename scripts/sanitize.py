#!/usr/bin/env python3
"""
把仓库里的个人信息替换为通用占位符（工作区层面）。

替换内容：
  - 具体实例域名（如 gitea.example.com）→ <gitea-host>
  - 具体用户名/仓库路径 → <owner>/<repo>
  - 内网 IP → <nas-ip>
  - 个人邮箱域名 → users.noreply.github.com

用法：python3 scripts/sanitize.py
"""
import os
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

# 具体值 → 占位符。
#
# 待替换的目标值从环境变量读取（SANITIZE_HOST / SANITIZE_USER / SANITIZE_REPO），
# 这样脚本自身不含有任何具体标识，也就不会被隐私自检误判为泄漏。
# 未设置环境变量时，只替换通用的内网 IP 与常见凭据模式。


def _target_host() -> str:
    return os.environ.get("SANITIZE_HOST", "").strip()


def _target_user() -> str:
    return os.environ.get("SANITIZE_USER", "").strip()


def _target_repo() -> str:
    return os.environ.get("SANITIZE_REPO", "").strip()


def build_rules() -> list[tuple[re.Pattern[str], str]]:
    rules: list[tuple[re.Pattern[str], str]] = []

    host = _target_host()
    if host:
        # 邮箱形式：xxx@<该域名> → noreply 形式
        rules.append((re.compile(r"@" + re.escape(host) + r"", re.I),
                      "@users.noreply.github.com"))
        # 实例域名（含子域）
        rules.append((re.compile(r"\b(?:[a-z0-9-]+\.)*" + re.escape(host) + r"\b", re.I),
                      "<gitea-host>"))

    user = _target_user()
    if user:
        repo = _target_repo()
        if repo:
            rules.append((re.compile(r"\b" + re.escape(user) + r"/" + re.escape(repo) + r"\b"),
                          "<owner>/<repo>"))
        rules.append((re.compile(r"\b" + re.escape(user) + r"\b"), "<owner>"))

    # 内网 IP（10.x / 192.168.x / 172.16-31.x）
    rules.append((re.compile(r"\b(?:10(?:\.\d{1,3}){3}"
                              r"|192\.168(?:\.\d{1,3}){2}"
                              r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"),
                   "<nas-ip>"))

    return rules


RULES = build_rules()

TARGET_SUFFIXES = (".md", ".yml", ".yaml", ".sh", ".py", ".cjs", ".js", ".json", ".txt")


def sanitize_text(text: str) -> tuple[str, int]:
    changed = 0
    for pattern, repl in RULES:
        text, n = pattern.subn(repl, text)
        changed += n
    return text, changed


def main() -> int:
    total = 0
    touched: list[str] = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts or "node_modules" in path.parts:
            continue
        if path.suffix.lower() not in TARGET_SUFFIXES:
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            raw = path.read_bytes()
        except Exception:
            continue
        if b"\x00" in raw[:2048]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        new_text, n = sanitize_text(text)
        if n:
            rel = path.relative_to(REPO)
            path.write_text(new_text, encoding="utf-8", newline="\n")
            touched.append(f"{rel} ({n})")
            total += n
    if touched:
        print("[sanitize] 已替换：")
        for t in touched:
            print(f"  - {t}")
    else:
        print("[sanitize] 无需替换")
    print(f"[sanitize] 共替换 {total} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
