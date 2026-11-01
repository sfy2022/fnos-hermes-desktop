#!/usr/bin/env bash
#
# 一键推送到 GitHub 并验证 CI
#
# 用法：
#   bash scripts/push.sh <github-username> [repo-name]
#
# 前提：本地已安装 gh CLI 并完成 `gh auth login`（或已配置 git 凭据）
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USERNAME="${1:-}"
REPO_NAME="${2:-fnos-hermes-desktop}"

log() { printf '\033[0;36m[push]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "${USERNAME}" ] || die "用法：bash scripts/push.sh <github-username> [repo-name]"
command -v gh >/dev/null 2>&1 || die "缺少 gh CLI，请先安装：https://cli.github.com"

cd "${REPO_ROOT}"

# ── 1. 确认登录 ─────────────────────────────────────────────────────────
if ! gh auth status >/dev/null 2>&1; then
  die "gh 未登录，请先执行：gh auth login"
fi
LOGIN_USER="$(gh api user --jq .login 2>/dev/null || echo "")"
log "已登录为 ${LOGIN_USER}"
if [ -n "${LOGIN_USER}" ] && [ "${LOGIN_USER}" != "${USERNAME}" ]; then
  die "登录账号(${LOGIN_USER}) 与指定用户名(${USERNAME})不一致"
fi

# ── 2. 检查远程仓库 ─────────────────────────────────────────────────────
REMOTE_URL="https://github.com/${USERNAME}/${REPO_NAME}.git"
if gh repo view "${USERNAME}/${REPO_NAME}" >/dev/null 2>&1; then
  log "远程仓库已存在：${USERNAME}/${REPO_NAME}"
else
  log "创建远程仓库 ${USERNAME}/${REPO_NAME}"
  gh repo create "${REPO_NAME}" \
    --description "Hermes 官方 Desktop 工作区的飞牛 NAS 原生应用，自动同步上游并构建 fpk" \
    --public \
    --disable-wiki \
    --disable-issues || die "创建失败（若仓库名被占用，脚本已存在则直接推送）"
fi

# ── 3. 配置远程并推送 ───────────────────────────────────────────────────
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${REMOTE_URL}"
else
  git remote add origin "${REMOTE_URL}"
fi
log "推送到 ${REMOTE_URL}"
git push -u origin main

# ── 4. 触发并观察 CI ────────────────────────────────────────────────────
log "触发构建工作流"
gh workflow run build.yml --repo "${USERNAME}/${REPO_NAME}" --ref main 2>/dev/null \
  || log "手动触发失败（可能尚未启用 Actions），稍后可在 Actions 页面手动运行"

echo
log "完成。接下来："
echo "  1. 打开 https://github.com/${USERNAME}/${REPO_NAME}/actions 查看构建进度"
echo "  2. 构建完成后从 Releases 下载 hermes-desktop.fpk"
echo "  3. 在飞牛 NAS：应用中心 → 手动安装 → 选择该 fpk"
echo
echo "提示：首次构建约需 5-10 分钟（要装 Node 依赖并编译渲染层）"
