#!/usr/bin/env bash
#
# 构建 fnOS Hermes Desktop 安装包（.fpk）
#
# 流程：
#   1. 克隆/更新上游 hermes-agent 源码
#   2. 用官方脚本构建 Webapp 渲染层（Desktop 的浏览器形态）
#   3. 把渲染层塞进 fpk 模板，并做打包前门禁（py_compile / 布局 / 网关契约）
#   4. 用 fnpack 打包成 .fpk
#
# 环境变量：
#   UPSTREAM_REPO   上游仓库（默认 NousResearch/hermes-agent）
#   UPSTREAM_REF    上游 ref（默认 main）
#   WORK_DIR        构建工作目录（默认 $PWD/.build）
#   OUTPUT_DIR      产物输出目录（默认 $PWD/dist）
#   SKIP_BUNDLE=1  跳过渲染层构建（复用已有 dist-webapp）
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_REPO="${UPSTREAM_REPO:-NousResearch/hermes-agent}"
UPSTREAM_REF="${UPSTREAM_REF:-main}"
WORK_DIR="${WORK_DIR:-${REPO_ROOT}/.build}"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/dist}"
FNPACK_VERSION="${FNPACK_VERSION:-1.2.3}"

SRC_DIR="${WORK_DIR}/hermes-agent"
FPK_DIR="${WORK_DIR}/fpk"
DIST_WEBAPP="${SRC_DIR}/apps/desktop/dist-webapp"

log() { printf '\033[0;36m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 工具检查 ─────────────────────────────────────────────────────────────
need() { command -v "$1" >/dev/null 2>&1 || die "缺少依赖：$1（$2）"; }
need git "请安装 git"
need curl "请安装 curl"
need tar "请安装 tar"
need node "官方构建脚本需要 Node >= 26"

NODE_MAJOR="$(node -v | sed 's/^v\([0-9]*\).*/\1/')"
if [ "${NODE_MAJOR}" -lt 26 ]; then
  die "Node 版本过低（当前 $(node -v)），官方 build-webapp.mjs 要求 >= 26"
fi
log "Node $(node -v)"

mkdir -p "${WORK_DIR}" "${OUTPUT_DIR}"

# ── 1. 上游源码 ─────────────────────────────────────────────────────────
log "同步上游 ${UPSTREAM_REPO}@${UPSTREAM_REF}"
# 克隆时强制 HTTP/1.1：GitHub 在 HTTP/2 下偶发 "RPC failed / HTTP2 framing error"
if [ -d "${SRC_DIR}/.git" ]; then
  git -C "${SRC_DIR}" -c http.version=HTTP/1.1 fetch --depth 1 origin "${UPSTREAM_REF}" \
    || git -C "${SRC_DIR}" fetch --depth 1 origin "${UPSTREAM_REF}"
  git -C "${SRC_DIR}" reset --hard "origin/${UPSTREAM_REF}"
else
  rm -rf "${SRC_DIR}"
  git -c http.version=HTTP/1.1 clone --depth 1 --branch "${UPSTREAM_REF}" \
    "https://github.com/${UPSTREAM_REPO}.git" "${SRC_DIR}" \
    || {
      # HTTP/1.1 仍失败时，改走 codeload / 代理源码包
      log "git clone 失败，改用源码包下载"
      mkdir -p "${WORK_DIR}/srcx"
      tgzs=(
        "https://codeload.github.com/${UPSTREAM_REPO}/tar.gz/refs/heads/${UPSTREAM_REF}"
        "https://gh-proxy.com/https://github.com/${UPSTREAM_REPO}/archive/refs/heads/${UPSTREAM_REF}.tar.gz"
        "https://ghproxy.net/https://github.com/${UPSTREAM_REPO}/archive/refs/heads/${UPSTREAM_REF}.tar.gz"
      )
      ok=0; u=""
      for u in "${tgzs[@]}"; do
        log "  尝试 $(echo "${u}" | cut -d/ -f3)"
        if curl -fsSL --retry 2 --max-time 600 -o "${WORK_DIR}/src.tar.gz" "${u}"; then
          ok=1
          break
        fi
      done
      [ "${ok}" = "1" ] || die "上游源码下载失败（GitHub / codeload / 代理均不可达）"
      tar xzf "${WORK_DIR}/src.tar.gz" -C "${WORK_DIR}/srcx"
      sub=""
      sub="$(find "${WORK_DIR}/srcx" -maxdepth 1 -mindepth 1 -type d | head -n 1)"
      [ -n "${sub}" ] || die "源码包解压异常"
      mv "${sub}" "${SRC_DIR}"
    }
fi

UPSTREAM_SHA="$(git -C "${SRC_DIR}" rev-parse --short HEAD 2>/dev/null || echo "")"
UPSTREAM_DATE="$(git -C "${SRC_DIR}" log -1 --format=%cI 2>/dev/null || echo "")"

# 源码包模式（无 .git）时，用 GitHub API 补齐 commit 信息以保持可追溯
if [ -z "${UPSTREAM_SHA}" ]; then
  UPSTREAM_SHA="$(curl -fsSL --max-time 30 \
    "https://api.github.com/repos/${UPSTREAM_REPO}/commits/${UPSTREAM_REF}" 2>/dev/null \
    | sed -n 's/.*"sha"[[:space:]]*:[[:space:]]*"\([0-9a-f]\{7,40\}\)".*/\1/p' | head -n 1)"
  UPSTREAM_SHA="${UPSTREAM_SHA:0:8}"
  UPSTREAM_DATE="$(curl -fsSL --max-time 30 \
    "https://api.github.com/repos/${UPSTREAM_REPO}/commits/${UPSTREAM_REF}" 2>/dev/null \
    | sed -n 's/.*"date"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  [ -n "${UPSTREAM_SHA}" ] || UPSTREAM_SHA="unknown"
  [ -n "${UPSTREAM_DATE}" ] || UPSTREAM_DATE="unknown"
fi
log "上游 commit ${UPSTREAM_SHA} (${UPSTREAM_DATE})"

# 版本号：浅克隆拿不到 tag，改用 GitHub API 查最新 release。
# 当显式指定了 tag 形式的 ref 时直接采用。
if [[ "${UPSTREAM_REF}" =~ ^v?[0-9] ]]; then
  HERMES_VERSION="${UPSTREAM_REF#v}"
  log "使用指定版本 ${HERMES_VERSION}"
else
  HERMES_VERSION="$(curl -fsSL --max-time 30 \
    "https://api.github.com/repos/${UPSTREAM_REPO}/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1)"
  if [ -z "${HERMES_VERSION}" ]; then
    HERMES_VERSION="$(date +%Y.%m.%d)"
    warn "未能从 API 获取版本号，回退到 ${HERMES_VERSION}"
  else
    log "上游最新版本 ${HERMES_VERSION}"
  fi
fi

# ── 1.5 注入 bundled 桌面插件 ───────────────────────────────────────────
# 浏览器的插件加载器只认**构建时打包**的插件：
#   src/apps/desktop/src/plugins/<id>/plugin.{js,tsx}  （vite glob）
# 磁盘上的 runtime door（<hermes home>/desktop-plugins/<id>/plugin.js）
# 依赖 window.hermesDesktop —— Electron 专属 IPC，浏览器里是 undefined，
# 所以纯 webapp 环境下磁盘插件一个都不会加载。
# 因此这里在构建前把随包插件放进源码树的对应位置，让它被 vite 打进产物。
inject_bundled_plugins() {
    local src_plugins="${REPO_ROOT}/packaging/fpk/app/plugins"
    local dst_plugins="${SRC_DIR}/apps/desktop/src/plugins"
    [ -d "${src_plugins}" ] || return 0
    mkdir -p "${dst_plugins}" 2>/dev/null || true

    local dir name
    for dir in "${src_plugins}"/*/; do
        [ -d "${dir}" ] || continue
        name="$(basename "${dir}")"
        # desktop/plugin.js 是 runtime door 形态；bundled 需要平铺的 plugin.js
        if [ -f "${dir}desktop/plugin.js" ]; then
            mkdir -p "${dst_plugins}/${name}" 2>/dev/null || true
            cp -f "${dir}desktop/plugin.js" "${dst_plugins}/${name}/plugin.js"
            log "注入 bundled 插件：${name}"
        fi
    done
}

if [ "${SKIP_BUNDLE:-0}" != "1" ]; then
    inject_bundled_plugins
fi

# ── 2. 构建 Webapp 渲染层 ───────────────────────────────────────────────
if [ "${SKIP_BUNDLE:-0}" = "1" ] && [ -f "${DIST_WEBAPP}/index.html" ]; then
  log "跳过渲染层构建（复用已有产物）"
else
  log "准备 Node 依赖（跳过 Electron 二进制下载）"
  ( cd "${SRC_DIR}" && node apps/desktop/scripts/build-webapp.mjs --deps-only )

  log "构建 Webapp 渲染层"
  ( cd "${SRC_DIR}" && node apps/desktop/scripts/build-webapp.mjs )
fi

if [ ! -f "${DIST_WEBAPP}/index.html" ]; then
  die "渲染层构建失败：未生成 ${DIST_WEBAPP}/index.html"
fi

# ── 2b. 注入默认语言 ────────────────────────────────────────────────────
# 上游 Webapp 的 i18n 只从浏览器 localStorage 的 "hermes-locale" 读取，
# 不读 config.yaml 的 display.language（那是给 CLI/TUI 用的）。
# 所以要在渲染层加载前写入 localStorage，才能让界面默认中文。
# 注意：只在用户尚未手动选择过时写入，不覆盖用户的选择。
DISPLAY_LANGUAGE="${DISPLAY_LANGUAGE:-zh}"
if [ -n "${DISPLAY_LANGUAGE}" ] && [ "${DISPLAY_LANGUAGE}" != "auto" ]; then
  inject_locale() {
    local html="${DIST_WEBAPP}/index.html"
    local marker="hermes-locale-boot"
    # 已注入过则跳过（幂等）
    if grep -q "${marker}" "${html}" 2>/dev/null; then
      log "默认语言已注入过，跳过"
      return 0
    fi
    # 插到主 bundle 的 <script type="module"> 之前，确保首屏就是目标语言
    python3 - "${html}" "${DISPLAY_LANGUAGE}" "${marker}" <<'PYEOF'
import sys, re
path, lang, marker = sys.argv[1], sys.argv[2], sys.argv[3]
html = open(path, encoding='utf-8').read()
if marker in html:
    sys.exit(0)
snippet = (
    f'    <!-- {marker}: 默认界面语言 -->\n'
    f'    <script>\n'
    f'      try {{\n'
    f'        if (!localStorage.getItem("hermes-locale")) {{\n'
    f'          localStorage.setItem("hermes-locale", "{lang}")\n'
    f'        }}\n'
    f'      }} catch {{ /* localStorage 不可用时忽略 */ }}\n'
    f'    </script>\n'
)
# 优先插在第一个 <script type="module" 之前；没有则插到 </head> 前
m = re.search(r'\s*<script type="module"', html)
if m:
    html = html[:m.start()] + "\n" + snippet + html[m.start():]
else:
    html = html.replace("</head>", snippet + "  </head>", 1)
open(path, "w", encoding="utf-8", newline="\n").write(html)
print(f"[build] 已注入默认语言 {lang}")
PYEOF
  }
  inject_locale || die "注入默认语言失败"
  log "默认界面语言：${DISPLAY_LANGUAGE}"
fi

BUNDLE_FILES="$(find "${DIST_WEBAPP}" -type f | wc -l | tr -d ' ')"
log "渲染层就绪：${BUNDLE_FILES} 个文件，$(du -sh "${DIST_WEBAPP}" | cut -f1)"

# ── 3. 组装 fpk 目录 ────────────────────────────────────────────────────
log "组装 fpk 目录"
rm -rf "${FPK_DIR}"
mkdir -p "${FPK_DIR}"
cp -r "${REPO_ROOT}/packaging/fpk/." "${FPK_DIR}/"

# 清理构建垃圾：包内不允许 __pycache__ / *.pyc / *.bak / .DS_Store
# （REFACTOR-CONTRACT §3.1「包根只允许以上条目」）。
find "${FPK_DIR}" \
    \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' \) -type d -prune \
    -exec rm -rf {} + 2>/dev/null || true
find "${FPK_DIR}" \
    \( -name '*.pyc' -o -name '*.pyo' -o -name '*.bak' -o -name '.DS_Store' \) -type f -delete \
    2>/dev/null || true

# 渲染层只允许构建注入：先清掉模板中可能残留的旧 app/webapp，再写入本次产物
rm -rf "${FPK_DIR}/app/webapp"
mkdir -p "${FPK_DIR}/app/webapp"
cp -r "${DIST_WEBAPP}/." "${FPK_DIR}/app/webapp/"

# 生命周期脚本必须可执行（fnpack 保留权限位；Windows 检出常丢失 +x）
chmod +x "${FPK_DIR}"/cmd/* 2>/dev/null || true
chmod +x "${FPK_DIR}"/app/runtime/gateway/*.py 2>/dev/null || true

# 版本号写入 manifest
if [ -f "${FPK_DIR}/manifest" ]; then
  # 应用版本默认跟随上游，但允许用 APP_VERSION 覆盖 —— 同版本号 fnOS 不允许
  # 覆盖安装，改了内容（如内置插件）后需要提版本，否则用户装不上。
  #
  # 注意：APP_VERSION 必须同时回写给 HERMES_VERSION。否则会出现
  # manifest 写 0.22.8、但产物文件名和 build-info.json 还是 0.22.5 的错位，
  # 下游（CI 的 release tag、下载文件名）全靠 build-info，一错全错。
  APP_VERSION="${APP_VERSION:-${HERMES_VERSION}}"
  HERMES_VERSION="${APP_VERSION}"
  sed -i.bak "s|^version[[:space:]]*=.*|version               = ${APP_VERSION}|" "${FPK_DIR}/manifest"
  sed -i.bak "s|^desc[[:space:]]*=.*|desc                  = Hermes 官方 Desktop 工作区的飞牛原生版。保留完整 Desktop 体验：对话流式输出、工具调用可视化、并排预览、文件浏览、Artifacts、定时任务、多 Profile、技能管理、终端栏、Git 审阅。基于官方 Webapp 形态（gateway 内建渲染层），无需 Electron，浏览器内即原生体验。上游 commit ${UPSTREAM_SHA}。|" "${FPK_DIR}/manifest"
  rm -f "${FPK_DIR}/manifest.bak"
fi

# ── 3.5 打包前门禁 ──────────────────────────────────────────────────────
# 1) 随包分发的 Python 运行时脚本必须语法正确：
#    adapter（薄网关适配层）与 pause_gateway（卸载/升级期优雅停机辅助）。
for py_rel in "app/runtime/gateway/gateway_adapter.py" "cmd/pause_gateway.py"; do
    py_file="${FPK_DIR}/${py_rel}"
    [ -f "${py_file}" ] || die "缺少运行时脚本：${py_file}"
    PYTHONPYCACHEPREFIX=/tmp/.build-pycache python3 -m py_compile "${py_file}" \
        2>/dev/null || die "Python 语法错误：${py_file}"
done
log "运行时脚本语法校验通过"

# 2) 结构与网关契约门禁（脚本由 QA 提供，接口固定：<script> <FPK_DIR>）
run_gate() {
    local rel="$1" script
    shift
    script="${REPO_ROOT}/${rel}"
    [ -f "${script}" ] || die "缺少校验脚本：${script}（由 QA 提供，见 REFACTOR-CONTRACT §6）"
    log "校验 ${rel}"
    python3 "${script}" "$@" || die "校验失败：${rel}"
}
run_gate "scripts/check_fpk_layout.py" "${FPK_DIR}"
run_gate "scripts/check_gateway_contract.py" "${FPK_DIR}"

# ── 4. 打包 ─────────────────────────────────────────────────────────────
log "获取 fnpack"
FNPACK="${WORK_DIR}/fnpack"
if [ ! -x "${FNPACK}" ]; then
  case "$(uname -m)" in
    x86_64)  FNPACK_TARGET="linux-amd64" ;;
    aarch64) FNPACK_TARGET="linux-arm64" ;;
    *) die "不支持的架构：$(uname -m)" ;;
  esac
  curl -fsSL --retry 3 --max-time 300 \
    -o "${FNPACK}" \
    "https://static2.fnnas.com/fnpack/fnpack-${FNPACK_VERSION}-${FNPACK_TARGET}"
  chmod +x "${FNPACK}"
fi
log "fnpack 已就绪（${FNPACK_VERSION}）"

log "打包 fpk"
( cd "${WORK_DIR}" && "${FNPACK}" build --directory "${FPK_DIR}" )

FPK_OUT="$(find "${WORK_DIR}" -maxdepth 1 -name '*.fpk' -newermt '-5 minutes' | head -n 1)"
[ -n "${FPK_OUT}" ] || FPK_OUT="$(find "${WORK_DIR}" -maxdepth 1 -name '*.fpk' | head -n 1)"
[ -n "${FPK_OUT}" ] || die "未生成 fpk"

# 产物用统一命名格式：产品名-平台-版本-上游commit
# 多版本并存时靠文件名即可分辨，不必再翻 manifest。
FINAL="${OUTPUT_DIR}/Hermes-Desktop-fnOS-v${HERMES_VERSION}-${UPSTREAM_SHA}.fpk"
cp -f "${FPK_OUT}" "${FINAL}"
# 保留一个固定名，便于脚本引用
cp -f "${FPK_OUT}" "${OUTPUT_DIR}/hermes-desktop.fpk"

# ── 5. 校验与元信息 ─────────────────────────────────────────────────────
SHA="$(sha256sum "${FINAL}" | cut -d' ' -f1)"
SIZE="$(stat -c%s "${FINAL}" 2>/dev/null || stat -f%z "${FINAL}")"

cat > "${OUTPUT_DIR}/build-info.json" <<EOF
{
  "version": "${HERMES_VERSION}",
  "upstream_repo": "${UPSTREAM_REPO}",
  "upstream_ref": "${UPSTREAM_REF}",
  "upstream_sha": "${UPSTREAM_SHA}",
  "upstream_date": "${UPSTREAM_DATE}",
  "bundle_files": ${BUNDLE_FILES},
  "fpk_name": "Hermes-Desktop-fnOS-v${HERMES_VERSION}-${UPSTREAM_SHA}.fpk",
  "fpk_size": ${SIZE},
  "fpk_sha256": "${SHA}",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

log "完成：${FINAL}"
log "  版本   ${HERMES_VERSION}（上游 ${UPSTREAM_SHA}）"
log "  大小   ${SIZE} 字节"
log "  SHA256 ${SHA}"
