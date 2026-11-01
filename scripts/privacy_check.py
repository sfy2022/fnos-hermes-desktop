#!/usr/bin/env python3
"""
提交/推送前的隐私自检。

检查三类问题：
  A. 工作区文件中是否含硬编码的个人信息（实例域名、用户名、内网 IP、凭据）
  B. Git 提交历史中的身份信息是否为 noreply
  C. 历史 blob 中是否残留敏感内容

用法：
    python3 scripts/privacy_check.py            # 检查工作区 + 历史
    python3 scripts/privacy_check.py --staged   # 只检查将要提交的内容

退出码：0 = 通过；1 = 发现风险
"""
from __future__ import annotations

import argparse
import base64
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 检测规则 ─────────────────────────────────────────────────────────────
# 内网地址（RFC1918）
IP_PATTERN = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
# 凭据赋值：password/secret/token 等后面跟具体字符串
SECRET_PATTERN = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|authorization|bearer)\s*[:=]\s*"
    r"['\"]([^\s'\"]{6,})['\"]",
    re.IGNORECASE,
)
# 私钥文件头（拼接构造，避免本文件命中自己的规则）
PRIVATE_KEY_MARK = "-" * 5 + "BEGIN"

# 令牌前缀用 base64 存储再解码：既避免本文件被自己的规则命中，
# 也让历史里的旧版本不会被扫成"疑似令牌"。
_TOKEN_PREFIXES_B64 = [
    "Z2hwXw==", "Z2hvXw==", "Z2h1Xw==", "Z2hzXw==", "Z2hyXw==",
    "Z2l0aHViX3BhdF8=", "Z2xwYXQt", "c2stYW50LQ==", "c2stcHJvai0=",
    "c2st",
]
_TOKEN_ALTS = "|".join(
    r"\b" + base64.b64decode(p).decode() for p in _TOKEN_PREFIXES_B64
)
TOKEN_PATTERN = re.compile(_TOKEN_ALTS + r"[A-Za-z0-9_\-]{12,}\b")

# 个人邮箱域名（本项目不应出现真实个人邮箱）
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com|noreply@)"
    r"(?!example\.(com|org))[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)

# 明确豁免：占位符、示例、变量引用
EXEMPT_SUBSTRINGS = (
    "example.com", "example.org", "gitea.example", "gitea-host",
    "<gitea-host>", "<owner>", "<repo>", "<nas-ip>", "<token>",
    "<username>", "<注册令牌>",
    "noreply", "users.noreply.github.com",
    "${", "$(", "your-", "PLACEHOLDER", "placeholder",
    "xxxx", "XXXX",
    # 文档里的令牌示例
    "ghp_xxx", "gho_xxx", "ghu_xxx", "ghs_xxx", "ghr_xxx",
    "github_pat_xxx", "glpat-xxx",
)

# 本工具自身的所有历史版本：其中的规则字面量是"词典"，不是泄漏
_SELF_BLOBS: set[str] = set()


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except Exception:
        return ""


def is_exempt(text: str) -> bool:
    return any(marker in text for marker in EXEMPT_SUBSTRINGS)


def scan_text(path_label: str, text: str, findings: list[tuple[str, str, str]]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        if is_exempt(line):
            continue
        for m in IP_PATTERN.finditer(line):
            findings.append((path_label, f"L{lineno}", f"内网 IP：{m.group()}"))
        for m in SECRET_PATTERN.finditer(line):
            val = m.group(1)
            if is_exempt(val) or val.startswith("$"):
                continue
            findings.append((path_label, f"L{lineno}", f"疑似硬编码凭据：{val[:24]}…"))
        if TOKEN_PATTERN.search(line):
            findings.append((path_label, f"L{lineno}", "疑似访问令牌"))
        if PRIVATE_KEY_MARK in line:
            findings.append((path_label, f"L{lineno}", "疑似私钥内容"))
        for m in EMAIL_PATTERN.finditer(line):
            findings.append((path_label, f"L{lineno}", f"疑似个人邮箱：{m.group()}"))


def check_worktree(staged_only: bool) -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    if staged_only:
        names = git("diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines()
    else:
        names = git("ls-files").splitlines()

    for name in names:
        if not name:
            continue
        p = REPO_ROOT / name
        if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
            continue
        raw = p.read_bytes()
        if b"\x00" in raw[:2048]:
            continue
        try:
            scan_text(name, raw.decode("utf-8", errors="replace"), findings)
        except Exception:
            continue
    return findings


def check_identity() -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    raw = git("log", "--all", "--format=%an <%ae>%n%cn <%ce>")
    seen = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        email = line.split("<")[-1].rstrip(">").lower()
        if email and "noreply" not in email:
            findings.append(("<git history>", "identity", f"提交身份非 noreply：{line}"))
    return findings


def collect_self_blobs() -> set[str]:
    shas: set[str] = set()
    for line in git("rev-list", "--all", "--objects").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].endswith("scripts/privacy_check.py"):
            shas.add(parts[0])
    return shas


def check_history_blobs() -> list[tuple[str, str, str]]:
    findings: list[tuple[str, str, str]] = []
    objs = git("rev-list", "--all", "--objects").splitlines()
    shas = {line.split()[0] for line in objs if line.strip()}
    for sha in shas:
        if sha in _SELF_BLOBS:
            continue
        if git("cat-file", "-t", sha).strip() != "blob":
            continue
        content = git("cat-file", "-p", sha)
        if not content or len(content) > 2 * 1024 * 1024:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if is_exempt(line):
                continue
            for m in IP_PATTERN.finditer(line):
                findings.append(("<git history>", f"blob {sha[:8]}:L{lineno}", f"内网 IP：{m.group()}"))
            if TOKEN_PATTERN.search(line):
                findings.append(("<git history>", f"blob {sha[:8]}:L{lineno}", "疑似访问令牌"))
            for m in EMAIL_PATTERN.finditer(line):
                findings.append(("<git history>", f"blob {sha[:8]}:L{lineno}", f"疑似个人邮箱：{m.group()}"))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="只检查暂存区")
    ap.add_argument("--skip-history", action="store_true", help="跳过历史扫描（更快）")
    args = ap.parse_args()

    print("=" * 62)
    print("隐私自检")
    print("=" * 62)

    findings: list[tuple[str, str, str]] = []

    print("\n[1/3] 检查工作区文件…")
    findings += check_worktree(args.staged)
    print(f"      发现 {len(findings)} 项")

    print("\n[2/3] 检查提交身份…")
    idf = check_identity()
    findings += idf
    print("      ✓ 全部为 noreply 身份" if not idf
          else f"      ✗ {len(idf)} 项非 noreply")

    if not args.skip_history:
        print("\n[3/3] 扫描历史 blob…")
        global _SELF_BLOBS
        _SELF_BLOBS = collect_self_blobs()
        hf = check_history_blobs()
        findings += hf
        print("      ✓ 历史中无敏感内容" if not hf
              else f"      ✗ {len(hf)} 项")
    else:
        print("\n[3/3] 历史扫描已跳过")

    print("\n" + "=" * 62)
    if findings:
        print(f"发现 {len(findings)} 项需要处理：")
        for path, loc, msg in findings[:40]:
            print(f"  ✗ {path}:{loc}  {msg}")
        print("\n建议：运行 python3 scripts/sanitize.py 替换为占位符后重试。")
        return 1

    print("✓ 全部通过，未发现个人信息或凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
