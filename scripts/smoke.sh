# 冒烟自检：不构建完整渲染层，验证流水线各环节是否正常
#
# 用途：改完打包脚本后快速验证（约 1 分钟），
#      确认"组装 fpk 目录 → 打包 → 校验"这条链路没被改坏。
#
# 用法：bash scripts/smoke.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="${WORK_DIR:-${REPO_ROOT}/.build-smoke}"

log() { printf '\033[0;36m[smoke]\033[0m %s\n' "$*"; }
die() { printf '\033[0;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

log "1/5 校验模板文件"
for f in manifest cmd/main cmd/install_callback cmd/install_init \
         cmd/upgrade_callback cmd/upgrade_init \
         cmd/uninstall_callback cmd/uninstall_init \
         cmd/config_callback cmd/config_init \
         config/privilege config/resource \
         wizard/install wizard/upgrade wizard/uninstall wizard/config \
         app/ui/config ICON.PNG ICON_256.PNG; do
  [ -f "${REPO_ROOT}/packaging/fpk/${f}" ] || die "缺少文件：packaging/fpk/${f}"
done
log "    模板完整"

log "2/5 校验 shell 脚本语法"
for f in "${REPO_ROOT}"/packaging/fpk/cmd/* "${REPO_ROOT}"/packaging/fpk/app/skills/fnos-trim/scripts/trim-cli; do
  bash -n "${f}" || die "语法错误：${f}"
done
log "    全部通过"

log "2b/5 校验换行符（Linux 执行，CRLF 会导致 bad interpreter）"
python3 - <<'PY'
import pathlib, sys
base = pathlib.Path("packaging/fpk")
targets = list((base / "cmd").iterdir())
targets += [base / "app/skills/fnos-trim/scripts/trim-cli"]
bad = []
for p in targets:
    if not p.is_file():
        continue
    raw = p.read_bytes()
    if b"\r\n" in raw:
        bad.append(str(p))
if bad:
    print("[fail] 以下文件含 CRLF：")
    for b in bad:
        print("       ", b)
    sys.exit(1)
print(f"[smoke]     {len(targets)} 个脚本均为 LF")
PY

log "3/5 校验 JSON 文件"
python3 - <<'PY'
import json, sys, pathlib
base = pathlib.Path("packaging/fpk")
for rel in ["app/ui/config", "config/privilege", "config/resource",
            "wizard/install", "wizard/upgrade", "wizard/uninstall", "wizard/config"]:
    p = base / rel
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[fail] JSON 错误 {rel}: {exc}")
        sys.exit(1)
print("[smoke]     JSON 全部合法")
PY

log "4/5 打包（用占位渲染层）"
rm -rf "${WORK_DIR}"
mkdir -p "${WORK_DIR}/fpk/app/webapp"
cp -r "${REPO_ROOT}/packaging/fpk/." "${WORK_DIR}/fpk/"
# 放一个最小的占位 index.html，只验证打包链路
printf '<!doctype html><title>smoke</title>\n' > "${WORK_DIR}/fpk/app/webapp/index.html"

FNPACK="${WORK_DIR}/fnpack"
case "$(uname -s)" in
  Linux)
    case "$(uname -m)" in
      x86_64)  T="linux-amd64" ;;
      aarch64) T="linux-arm64" ;;
      *) echo "跳过打包（架构 $(uname -m) 无预编译 fnpack）"; exit 0 ;;
    esac
    ;;
  MINGW*|MSYS*|CYGWIN*) T="windows-amd64" ;;
  Darwin) T="darwin-amd64" ;;
  *) echo "跳过打包（系统 $(uname -s) 无预编译 fnpack）"; exit 0 ;;
esac
curl -fsSL --retry 2 --max-time 120 -o "${FNPACK}" \
  "https://static2.fnnas.com/fnpack/fnpack-1.2.3-${T}" || die "fnpack 下载失败"
chmod +x "${FNPACK}" 2>/dev/null || true
( cd "${WORK_DIR}" && "${FNPACK}" build --directory "${WORK_DIR}/fpk" ) || die "打包失败"

log "5/5 校验产物"
FPK="$(find "${WORK_DIR}" -maxdepth 1 -name '*.fpk' | head -n 1)"
[ -n "${FPK}" ] || die "未生成 fpk"
python3 - "$FPK" <<'PY'
import sys, tarfile, io
with tarfile.open(sys.argv[1]) as t:
    names = [m.name for m in t.getmembers()]
    assert "manifest" in names, "缺少 manifest"
    assert "cmd/main" in names, "缺少 cmd/main"
    assert "wizard/install" in names, "缺少 wizard/install"
    mf = t.extractfile("manifest").read().decode()
    assert "appname" in mf and "service_port" in mf, "manifest 字段缺失"
    print(f"[smoke]     包内 {len(names)} 个条目，校验通过")
PY

rm -rf "${WORK_DIR}"
log "冒烟测试全部通过"
