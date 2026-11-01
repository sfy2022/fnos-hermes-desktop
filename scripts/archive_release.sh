#!/usr/bin/env bash
#
# 归档构建产物到下载目录，并清理工作目录里的中间产物。
#
# 背景：开发过程中会产出多个 fpk 版本，容易搞混哪个是最新。
# 本脚本把最终产物以「产品名-平台-版本-日期」命名放进下载目录，
# 并把工作目录里的历史 fpk 收进 archive/ 归档（不直接删除）。
#
# 用法：
#   bash scripts/archive_release.sh <fpk路径> [版本号]
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE="$(cd "${REPO_ROOT}/.." && pwd)"
FPK_SRC="${1:-}"
VERSION="${2:-}"

log() { printf '\033[0;36m[archive]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "${FPK_SRC}" ] || die "用法：bash scripts/archive_release.sh <fpk路径> [版本号]"
[ -f "${FPK_SRC}" ] || die "找不到文件：${FPK_SRC}"

# 下载目录：Windows 用 ~/Downloads，Linux/macOS 用 ~/Downloads
DOWNLOAD_DIR="${HOME}/Downloads"
mkdir -p "${DOWNLOAD_DIR}" 2>/dev/null || DOWNLOAD_DIR="${HOME}"
[ -d "${DOWNLOAD_DIR}" ] || die "无法确定下载目录"

# 版本号：优先用参数，否则从包内 manifest 读
if [ -z "${VERSION}" ]; then
  # manifest 是 key=value 形式（值可能带引号也可能不带）
  VERSION="$(tar xzOf "${FPK_SRC}" manifest 2>/dev/null \
            | grep -E '^[[:space:]]*version[[:space:]]*=' | head -1 \
            | cut -d= -f2- | tr -d '"[:space:]' || true)"
fi
[ -n "${VERSION}" ] || VERSION="unknown"

DATE="$(date +%Y%m%d)"
DEST="${DOWNLOAD_DIR}/Hermes-Desktop-fnOS-v${VERSION}-${DATE}.fpk"

log "源文件  ${FPK_SRC}"
log "下载目录 ${DOWNLOAD_DIR}"
log "目标    ${DEST}"

# 同名目标先备份，不直接覆盖
if [ -f "${DEST}" ]; then
  mv "${DEST}" "${DEST}.bak"
  log "已存在同名文件，备份为 ${DEST}.bak"
fi

cp "${FPK_SRC}" "${DEST}"

# 校验一致性
SRC_SUM="$(sha256sum "${FPK_SRC}" | cut -d' ' -f1)"
DST_SUM="$(sha256sum "${DEST}" | cut -d' ' -f1)"
if [ "${SRC_SUM}" != "${DST_SUM}" ]; then
  die "校验失败：源 ${SRC_SUM} ≠ 目标 ${DST_SUM}"
fi

log "✓ 已归档（SHA256 ${DST_SUM}）"
log "  ${DEST}"

# 清理下载目录里上一版的同名前缀文件（保留 .bak 与本文件）
log "清理下载目录里的旧版本（保留 .bak）"
find "${DOWNLOAD_DIR}" -maxdepth 1 -type f \
     -name 'Hermes-Desktop-fnOS-*.fpk' ! -name "$(basename "${DEST}")" \
     -print -delete 2>/dev/null | while read -r f; do
  log "  已删除 $(basename "${f}")"
done

# 工作目录里的历史 fpk 归档到 archive/，不直接删
ARCHIVE_DIR="${WORKSPACE}/archive/fpk"
mkdir -p "${ARCHIVE_DIR}"
log "归档工作目录里的历史 fpk 到 archive/fpk/"
find "${WORKSPACE}" -maxdepth 1 -type f -name '*.fpk' \
     ! -name "$(basename "${FPK_SRC}")" \
     -print 2>/dev/null | while read -r f; do
  mv "${f}" "${ARCHIVE_DIR}/" 2>/dev/null && log "  $(basename "${f}") → archive/fpk/"
done

log "完成"
