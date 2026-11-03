#!/bin/bash
#
# 预置缺失的运行时库（不需要 root）
#
# 背景：pm 默认会装 node 工具链，但官方 node 二进制依赖 libatomic.so.1，
# 而 fnOS 精简系统没有这个库：
#   ✗ node: staged entry failed verification: .../bin/node --version exited 127:
#     error while loading shared libraries: libatomic.so.1:
#     cannot open shared object file: No such file or directory
#
# pm 装 node 失败会导致整个 pm install 报错回退到 uv pip install，
# 既慢（多装 chromium 等重依赖）又丢掉 pm 自动剥离 exclude-newer 的好处。
#
# 这里用 apt-get download（不需要 root，只下载不安装）+ dpkg -x 解包，
# 把 libatomic.so.1 放到应用自己的 lib 目录，再通过 LD_LIBRARY_PATH 生效。
#
# fnOS 自带的 nodejs_v24 应用虽然自带 node，但 pm 采取 store-first 策略
# （把自己下载的版本放在 PATH 最前），所以光加 PATH 挡不住它下载。
#
# 位置：随包分发的只读组件（target/runtime/lib/libs.sh），由 install_callback
# 等生命周期脚本 source 使用；调用方需先定义 DATA_DIR 与 log_msg。

LIB_DIR="${DATA_DIR}/lib"

ensure_libatomic() {
    # 已经能跑就不管
    if command -v node >/dev/null 2>&1 && node --version >/dev/null 2>&1; then
        return 0
    fi
    # 已有我们预置的库且有效
    if [ -f "${LIB_DIR}/libatomic.so.1" ]; then
        export LD_LIBRARY_PATH="${LIB_DIR}:${LD_LIBRARY_PATH:-}"
        return 0
    fi

    mkdir -p "${LIB_DIR}" 2>/dev/null || return 1

    local tmp
    tmp="$(mktemp -d 2>/dev/null)" || return 1

    # apt-get download 只下载 .deb 到当前目录，不需要 root
    if ( cd "${tmp}" && apt-get download libatomic1 >/dev/null 2>&1 ); then
        local deb
        deb="$(ls "${tmp}"/libatomic1_*.deb 2>/dev/null | head -1)"
        if [ -n "${deb}" ] && command -v dpkg-deb >/dev/null 2>&1; then
            dpkg-deb -x "${deb}" "${tmp}/x" 2>/dev/null
            local so
            so="$(find "${tmp}/x" -name 'libatomic.so.1*' -type f 2>/dev/null | head -1)"
            if [ -n "${so}" ]; then
                cp "${so}" "${LIB_DIR}/libatomic.so.1" 2>/dev/null || true
                # 同时带上版本化文件与符号链接目标
                local ver
                ver="$(basename "${so}")"
                [ "${ver}" != "libatomic.so.1" ] && cp "${so}" "${LIB_DIR}/${ver}" 2>/dev/null || true
                export LD_LIBRARY_PATH="${LIB_DIR}:${LD_LIBRARY_PATH:-}"
                log_msg "已预置 libatomic.so.1 到 ${LIB_DIR}"
            fi
        fi
    fi

    rm -rf "${tmp}" 2>/dev/null || true
    return 0
}
