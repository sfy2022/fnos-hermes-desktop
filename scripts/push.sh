#!/usr/bin/env bash
#
# 推送到 GitHub 或 Gitea，并触发 CI
#
# 用法：
#   bash scripts/push.sh                      # 用当前 remote
#   bash scripts/push.sh gitea                # 推到你自建的 Gitea
#   bash scripts/push.sh github <username>    # 推到 GitHub
#
# 前置：
#   Gitea  → ~/.git-credentials 里已有凭据
#   GitHub → 已安装 gh CLI 并 `gh auth login`
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${1:-}"
USER_NAME="${2:-}"

log() { printf '\033[0;36m[push]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

cd "${REPO_ROOT}"

# ── 目标平台 ─────────────────────────────────────────────────────────────
GITHUB_USER=""
GITEA_HOST=""
GITEA_USER=""
REPO_NAME="$(basename "${PWD}")"

if [ "${PLATFORM}" = "github" ]; then
    command -v gh >/dev/null 2>&1 || die "缺少 gh CLI：https://cli.github.com"
    gh auth status >/dev/null 2>&1 || die "请先执行 gh auth login"
    GITHUB_USER="${USER_NAME:-$(gh api user --jq .login 2>/dev/null)}"
    [ -n "${GITHUB_USER}" ] || die "无法确定 GitHub 用户名"
    REMOTE_URL="https://github.com/${GITHUB_USER}/${REPO_NAME}.git"
    WEB_URL="https://github.com/${GITHUB_USER}/${REPO_NAME}"
elif [ "${PLATFORM}" = "gitea" ]; then
    GITEA_HOST="${GITEA_HOST:-<gitea-host>}"
    GITEA_USER="${USER_NAME:-$(grep -m1 -oE 'https://[^:]+:' ~/.git-credentials 2>/dev/null | sed 's|https://||; s|:||')}"
    [ -n "${GITEA_USER}" ] || die "无法从 ~/.git-credentials 推断 Gitea 用户名，请显式指定"
    REMOTE_URL="https://${GITEA_HOST}/${GITEA_USER}/${REPO_NAME}.git"
    WEB_URL="https://${GITEA_HOST}/${GITEA_USER}/${REPO_NAME}"
else
    REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
    [ -n "${REMOTE_URL}" ] || die "未指定平台且没有 origin remote。用法：bash scripts/push.sh [gitea|github] [username]"
    WEB_URL="${REMOTE_URL%.git}"
    log "沿用现有 remote：${REMOTE_URL}"
fi

# ── 配置 remote ─────────────────────────────────────────────────────────
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "${REMOTE_URL}"
else
    git remote add origin "${REMOTE_URL}"
fi
log "remote → ${REMOTE_URL}"

# ── 推送 ────────────────────────────────────────────────────────────────
log "推送 main 分支"
git push -u origin main

# ── 触发 CI ─────────────────────────────────────────────────────────────
if [ "${PLATFORM}" = "github" ]; then
    log "触发 GitHub Actions"
    gh workflow run build.yml --repo "${GITHUB_USER}/${REPO_NAME}" --ref main 2>/dev/null \
        || log "手动触发失败，可稍后在 Actions 页面手动运行"
elif [ "${PLATFORM}" = "gitea" ]; then
    TOKEN="${GITEA_TOKEN:-}"
    if [ -z "${TOKEN}" ] && command -v tea >/dev/null 2>&1; then
        TOKEN="$(tea login list 2>/dev/null | awk '/Token/ {print $2; exit}')" || true
    fi
    if [ -n "${TOKEN}" ]; then
        log "触发 Gitea Actions"
        curl -s -H "Authorization: token ${TOKEN}" \
          -H "Content-Type: application/json" \
          -X POST "https://${GITEA_HOST}/api/v1/repos/${GITEA_USER}/${REPO_NAME}/actions/workflows/build.yml/dispatches" \
          -d '{"ref":"main"}' >/dev/null && echo "✓ 已触发" || echo "触发失败"
    else
        log "未找到 Gitea token，跳过自动触发（可手动在 Actions 页面运行）"
    fi
fi

echo
log "完成：${WEB_URL}"
echo "  1. 打开 ${WEB_URL}/actions 查看构建状态"
echo "  2. 构建完成后从 Releases 下载 hermes-desktop.fpk"
echo "  3. 飞牛 NAS：应用中心 → 手动安装 → 选择该 fpk"
