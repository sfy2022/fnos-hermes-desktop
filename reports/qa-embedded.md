# QA 报告：内嵌修复与 FPK 目录规范化

> 分支：`refactor/embedded-gateway` ｜ 基线：`main@9ea1f57`
> 环境：Windows 开发机 + e5 真机（fnOS/Debian 12，x86_64，SSH `sun@192.168.1.2`）
> 结论：代码与适配层验证通过；真机安装/内嵌链路见 §5（按实际执行结果更新）。

## 1. 根因与修复

- 上游 Webapp 返回 CSP `frame-ancestors 'none'`（commit `8d91e48e` 的
  `hermes_cli/webapp_page_policy.py`），浏览器拒绝所有 iframe。
- 旧方案改写 HTML/注入内联 bridge，破坏同一 CSP 的内联脚本 sha256 白名单。
- 新方案：薄适配层 + 上游原生 `X-Forwarded-Prefix`，**响应体零改写**；
  仅将 `frame-ancestors 'none'` 改为 `'self'`、`X-Frame-Options: DENY` 改为 `SAMEORIGIN`。
  直连 `:9120` 不经过适配层，CSP 保持 `'none'`。

## 2. 自动化验证

| 项 | 命令 | 结果 |
|---|---|---|
| 单元/集成测试（Windows） | `python -m unittest discover -s tests` | 52 tests OK（12 个 AF_UNIX 用例 skip） |
| 单元/集成测试（e5 Linux） | `python3 -m unittest discover -s tests` | **52 tests OK（全量，含 AF_UNIX/WS）** |
| 布局检查（源码/模板） | `python3 scripts/check_fpk_layout.py packaging/fpk --template` | exit 0，违规 0；警告：缺 `app/webapp`（构建注入）、含 arm64 二进制 |
| 网关契约 | `python3 scripts/check_gateway_contract.py packaging/fpk` | exit 0，违规/警告 0 |
| Shell 语法（e5） | `bash -n` 全部 cmd/runtime/scripts | 通过 |
| Python 语法 | `py_compile gateway_adapter.py pause_gateway.py` | 通过 |

关键断言（`tests/gateway/test_adapter.py`）：

- 上游 HTML 响应体**逐字节相等**（适配层不改写）；
- CSP 仅 `frame-ancestors` 变化，sha256 哈希/`base-uri`/`form-action` 原样；
- `Location` 与 `Set-Cookie` 原样（不做二次前缀）；
- `X-Forwarded-Prefix` 注入并覆盖客户端伪造值；
- WebSocket 101 + 帧回显；SSE 首事件 <0.5s；chunked 请求解码；HEAD/204/304；超限 431/413。

## 3. e5 适配层冒烟（不依赖 fpk 安装）

脚本：`/tmp/adapter_smoke.py`、`/tmp/ws_smoke.py`（临时文件，不入库）

| 用例 | 结果 |
|---|---|
| CSP `frame-ancestors 'none'` → `'self'`，sha256/base-uri 保留 | PASS |
| 响应体逐字节不变 | PASS |
| `X-Forwarded-Prefix` / `Origin` 重写 / Cookie 透传 | PASS |
| Location `/login?next=%2F` 原样、Set-Cookie 原样 | PASS |
| POST body、健康端点、SSE 流式 | PASS |
| WebSocket 握手 101 + 帧回显 + XFP/Origin 改写 | PASS |

## 4. FPK 构建

- 因 e5 到 GitHub 的 git clone 极慢（上游仓库约 1.4GB），构建用**当前 release 的
  `dist-webapp`**（v0.22.12，`app.tgz` / `webapp`）作为渲染层，与本仓库 `packaging/fpk`
  组装后经 `fnpack 1.2.4` 打包，用于验证适配层/目录/安装链路；生产构建仍走
  `scripts/build.sh` + CI（Node 26）。
- 组装包通过 `check_fpk_layout.py`（违规 0，仅 `PKG-ARCH-03` 臂警告）、
  `check_gateway_contract.py`（0/0）、`py_compile`。
- 产物：`/tmp/hermes-build/hermes-desktop.fpk`（约 22MB，manifest `0.22.6`，测试首装）；
  另外构建了 `0.22.13` 升级包 `/tmp/hermes-build/upgrade-out/hermes-desktop.fpk`。
- 构建过程中发现并修复两个真实问题：
  1. 组装脚本把 `__pycache__` 带进包（已改为 `PYTHONPYCACHEPREFIX` 并清理）；
  2. **首装时 venv 尚未建立，scrypt 哈希生成失败 → 配置回退 `host=127.0.0.1`（局域网直连失效）**。
     已在 `install_callback` 增加 `finalize_webapp_auth`（装完依赖后补哈希并恢复 `0.0.0.0`），
     并在 `cmd/main` 增加运行期自愈（升级后 start 也会补）。

## 5. 真机安装与内嵌验收（e5）

（按实际执行结果填写：`appcenter-cli install-fpk --env`、start、CSP 两级、WS、升级/卸载。）

- 安装：
- 启动：
- 直连 `:9120` CSP：
- 适配层 socket CSP：
- 健康端点：
- 升级迁移：
- 卸载清理：

## 6. 证据与产物

- 分支：`refactor/embedded-gateway`（已推送；PR 创建受限于当前 GitHub token 只读，
  手动入口：`https://github.com/sfy2022/fnos-hermes-desktop/pull/new/refactor/embedded-gateway`）
- 构建：`/tmp/hermes-build/*.fpk`（e5）
- 日志：`/tmp/makefpk.log`、`/tmp/hermes-tests.log`

## 7. 未验证/风险

- 浏览器内真实 iframe 渲染（e5 无浏览器；CSP 头与 WS 已由 curl/脚本验证）。
- 内嵌仍需 Hermes 自身登录一次；不做飞牛 SSO。
- 上游升级若再次调整 CSP/前缀行为，需按 `docs/EMBEDDING.md` 回归。
- Windows 构建的 `libatomic`/Node 26 问题只影响本地构建机，不影响 fnOS 运行。
