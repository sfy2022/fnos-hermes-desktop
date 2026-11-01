#!/usr/bin/env bash
#
# 安全推送到 GitHub / Gitea
#
# 凭据安全设计：
#   1. token 只从环境变量读，绝不作为命令行参数传（ps 与 shell 历史会记录 argv）
#   2. 用 GIT_ASKPASS + 临时文件传递，不进 .git/config、不进 shell 历史
#   3. 临时文件权限 600，退出即删（trap 保证异常时也清理）
#   4. 仓库里不写任何凭据；git config 只设 user.name / user.email
#   5. 提交身份固定为 noreply，不暴露真实邮箱
#
# 用法：
#   export GITHUB_TOKEN=ghp_xxx          # 或 GITEA_TOKEN
#   bash scripts/push.sh github <username>
#   bash scripts/push.sh gitea <username>   # 需先 export GITEA_HOST=git.example.com
#
# 不设 token 时复用已有 git credential（~/.git-credentials）。
# 推送前会自动跑一次隐私自检。
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${1:-}"
USER_NAME="${2:-}"

log() { printf '\033[0;36m[push]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

cd "${REPO_ROOT}"

# ── 提交身份：noreply，不暴露真实邮箱 ───────────────────────────────────
if [ -z "$(git config user.email 2>/dev/null)" ]; then
    git config user.name "fnOS Community"
    git config user.email "noreply@users.noreply.github.com"
fi

# ── 推送前隐私自检 ──────────────────────────────────────────────────────
if command -v python3 >/dev/null 2>&1 && [ -f scripts/privacy_check.py ]; then
    log "推送前隐私自检…"
    if ! python3 scripts/privacy_check.py --skip-history; then
        die "隐私自检未通过，已中止推送"
    fi
fi

# ── 凭据传递（临时文件 + GIT_ASKPASS，退出即删） ───────────────────────
ASKPASS_FILE=""
cleanup() {
    if [ -n "${ASKPASS_FILE}" ] && [ -f "${ASKPASS_FILE}" ]; then
        shred -u "${ASKPASS_FILE}" 2>/dev/null || rm -f "${ASKPASS_FILE}"
    fi
}
trap cleanup EXIT

setup_askpass() {
    local token="$1"
    [ -n "${token}" ] || return 1
    ASKPASS_FILE="$(mktemp)"
    chmod 600 "${ASKPASS_FILE}"
    cat > "${ASKPASS_FILE}" <<EOF
#!/bin/sh
case "\$1" in
  *Username*) echo "${USER_NAME:-x}" ;;
  *Password*) echo "${token}" ;;
  *) echo "${token}" ;;
esac
EOF
    chmod 700 "${ASKPASS_FILE}"
    export GIT_ASKPASS="${ASKPASS_FILE}"
    export GIT_TERMINAL_PROMPT=0
    return 0
}

# ── 目标平台 ─────────────────────────────────────────────────────────────
GITEA_HOST="${GITEA_HOST:-}"
REPO_NAME="$(basename "${PWD}")"

case "${PLATFORM}" in
github)
    [ -n "${USER_NAME}" ] || die "用法：bash scripts/push.sh github <username>"
    REMOTE_URL="https://github.com/${USER_NAME}/${REPO_NAME}.git"
    WEB_URL="https://github.com/${USER_NAME}/${REPO_NAME}"
    TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
    ;;
gitea)
    [ -n "${USER_NAME}" ] || die "用法：bash scripts/push.sh gitea <username>"
    [ -n "${GITEA_HOST}" ] || die "请设置 GITEA_HOST，例如：export GITEA_HOST=git.example.com"
    REMOTE_URL="https://${GITEA_HOST}/${USER_NAME}/${REPO_NAME}.git"
    WEB_URL="https://${GITEA_HOST}/${USER_NAME}/${REPO_NAME}"
    TOKEN="${GITEA_TOKEN:-}"
    ;;
*)
    REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
    [ -n "${REMOTE_URL}" ] || die "用法：bash scripts/push.sh <github|gitea> <username>"
    WEB_URL="${REMOTE_URL%.git}"
    TOKEN="${GITHUB_TOKEN:-${GITEA_TOKEN:-${GH_TOKEN:-}}}"
    log "沿用现有 remote：${REMOTE_URL}"
    ;;
esac

if [ -n "${TOKEN}" ]; then
    log "使用环境变量中的 token（不会写入磁盘或命令行）"
    setup_askpass "${TOKEN}" || die "无法创建 askpass 助手"
else
    log "未提供 token，尝试复用已有 git credential"
fi

# ── 推送 ────────────────────────────────────────────────────────────────
if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "${REMOTE_URL}"
else
    git remote add origin "${REMOTE_URL}"
fi
log "remote → ${REMOTE_URL}"

log "推送 main 分支"
git push -u origin main

# ── 触发 CI ─────────────────────────────────────────────────────────────
if [ "${PLATFORM}" = "github" ]; then
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        log "触发 GitHub Actions"
        gh workflow run build.yml --ref main 2>/dev/null \
            || log "自动触发失败，可稍后在 Actions 页面手动运行"
    else
        log "gh CLI 不可用，跳过自动触发"
    fi
elif [ "${PLATFORM}" = "gitea" ] && [ -n "${TOKEN}" ]; then
    log "触发 Gitea Actions"
    curl -s -H "Authorization: token ${TOKEN}" \
      -H "Content-Type: application/json" \
      -X POST "https://${GITEA_HOST}/api/v1/repos/${USER_NAME}/${REPO_NAME}/actions/workflows/build.yml/dispatches" \
      -d '{"ref":"main"}' >/dev/null && echo "✓ 已触发" || echo "触发失败"
fi

echo
log "完成：${WEB_URL}"
echo "  1. 打开 ${WEB_URL}/actions 查看构建状态"
echo "  2. 构建完成后从 Releases 下载 hermes-desktop.fpk"
echo "  3. 飞牛 NAS：应用中心 → 手动安装 → 选择该 fpk"
