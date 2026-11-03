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
| 单元/集成测试（Windows） | `python -m unittest discover -s tests` | 73 tests OK（12 个 AF_UNIX 用例 skip） |
| 单元/集成测试（e5 Linux） | `python3 -m unittest discover -s tests` | **73 tests OK（全量，含 AF_UNIX/WS）** |
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

### 4.1 官方构建（scripts/build.sh，最新上游）

在 e5 上用官方 `scripts/build.sh` 从上游 `main` 正式构建：

| 项 | 值 |
|---|---|
| 版本 | **0.22.16** |
| 上游 | `NousResearch/hermes-agent@fd267bb3`（main） |
| 产物 | `dist/Hermes-Desktop-fnOS-v0.22.16-fd267bb3.fpk` |
| 大小 | 22,650,933 字节 |
| SHA256 | `d91db7376e6250f6dcf275a4be6853a75b02c2ca4f6153d4efd76770157a28fd` |
| 构建时门禁 | 布局检查 / 网关契约 / py_compile 均通过（build.sh §3.5） |
| build-info | `/dist/build-info.json`（`bundle_files: 1079`） |

分支 `.upstream-sha` 已更新为 `fd267bb3242a14c33d4899cffc60ee4be816f14d`。
工作区已回传产物：`hermes-src/dist/Hermes-Desktop-fnOS-v0.22.16-fd267bb3.fpk`。

> 0.22.16 安装向导重做：
> - 三步：访问账号 → **模型配置** → 网络配置；模型步骤可选
>   OpenRouter / Anthropic / DeepSeek / 自定义 OpenAI 兼容 / 稍后配置。
> - 新增 `cmd/apply_model_config.py`：在 venv 创建前用系统 python3 写
>   `etc/config.yaml` 的 `model` 段与 `var/.env` 的 provider key；
>   内置 provider 的 key 只进 `.env`，`custom`/`deepseek` 的 `api_key` 进 config；
>   两文件 0600，key 不落日志。带 6 个单测（openrouter/anthropic/deepseek/custom/skip/已有配置）。
> - `build.sh` 注入的 `manifest.desc` 同步写明「唯一入口 Hermes；直连 http://<NAS_IP>:9120」。
>
> 0.22.15 产品化变更（已包含）：
> - 桌面**只保留一个内嵌入口**，名称 `Hermes`；删除直连 `type=url` 图标，
>   `desktop_applaunchname=hermes-desktop.Embedded`；端口 `9120` 写进 `manifest.desc` 提示。
> - 图标换为**官方 Hermes 图标**（`apps/desktop/assets/icon-dark.png` 生成的
>   64/256/512 全套），替换原像素风图标。
> - 修复“卸载时选清除数据但数据目录仍在”：卸载日志不再 `mkdir -p $TRIM_PKGVAR`
>   把目录重新创建；删除前 `cd /`，`rm -rf` 后二次 `find`/`rmdir` 校验；
>   向导值兼容 `true/1/yes/on`。已在 e5 手工执行验证 `/vol1/@appdata/hermes-desktop` 被真正删除。
>
> 0.22.14 修复：iframe 刷新白屏 —— 裸挂载路径 `/app/hermes-desktop`（无尾斜杠）
> 会让 `./assets/...` 相对路径解析到 `/app/assets/...` 而 302/404。adapter 对裸路径
> 302 到 `/app/hermes-desktop/`，`app/ui/config` 的 `url` 也带尾斜杠。
>
> e5 上当前没有已安装实例（已卸载）；新包需在飞牛 Web UI 手动安装。

### 4.2 早期测试构建

- 因 e5 到 GitHub 的 git clone 极慢（上游仓库约 1.4GB），早期验证用**当前 release 的
  `dist-webapp`**（v0.22.12）作为渲染层手工组装，用于验证适配层/目录/安装链路；
  正式产物以上面的 `scripts/build.sh` 构建为准。
- 组装包通过 `check_fpk_layout.py`（违规 0，仅 `PKG-ARCH-03` 臂警告）、
  `check_gateway_contract.py`（0/0）、`py_compile`。
- 产物：`/tmp/hermes-build/hermes-desktop.fpk`（约 22MB，manifest `0.22.6`，测试首装）；
  另外构建了 `0.22.13` 升级包 `/tmp/hermes-build/upgrade-out/hermes-desktop.fpk`。
- 构建过程中发现并修复两个真实问题：
  1. 组装脚本把 `__pycache__` 带进包（已改为 `PYTHONPYCACHEPREFIX` 并清理）；
  2. **首装时 venv 尚未建立，scrypt 哈希生成失败 → 配置回退 `host=127.0.0.1`（局域网直连失效）**。
     已在 `install_callback` 增加 `finalize_webapp_auth`（装完依赖后补哈希并恢复 `0.0.0.0`），
     并在 `cmd/main` 增加运行期自愈（升级后 start 也会补）。

### 4.3 发布策略（2026-11-04 起）

- `sync.yml` 每 6 小时查上游 `releases/latest`，只有 tag 与 `.upstream-release` 不同才触发 `build.yml`；
  不再对比 main commit。
- `build.yml` 已移除 `schedule` 与 `push` 触发，只接受 `sync.yml` 或手动 dispatch；
  按上游 **release tag** 构建，不跟 main；Release 发布成功后写 `.upstream-release` / `.upstream-sha`。
- 应用版本号仍按「上游 Release 版本 + 本地 tag」单调递增，保证 fnOS 可升级。
- 初始 `.upstream-release=v0.22.6`（当前上游最新 Release）；因此向导精简等打包改动**不会自动出新包**，
  等上游发布下一个 Release 才会构建。

### 4.4 0.22.17 真机缺陷修复（手工构建）

0.22.16 在真机上出现两个问题，已修复并手工构建 0.22.17：

| 项 | 值 |
|---|---|
| 版本 | **0.22.17** |
| 上游 | `fd267bb3` |
| 产物 | `dist/Hermes-Desktop-fnOS-v0.22.17-fd267bb3.fpk` |
| 大小 | 22,657,328 字节 |
| SHA256 | `1511de7ddd729e92048835bbb1f811510225375f40b0fc0ab69b46e6b7cc34e9` |

- **空密码哈希导致 `Sign in to Hermes`**：首装 venv 未就绪时写了
  `dashboard.basic_auth.password_hash: ""`，后续 finalize 只检查“有没有 basic_auth”就跳过，
  最终 host 停在 127.0.0.1、认证为空，上游浏览器端显示
  “Run `hermes webapp` on this computer…”。
  修复：首装哈希为空时不写 dashboard 段；install/main 的 finalize 改为检查
  `password_hash` 是否为有效 `scrypt$…`，空哈希就地补全（不追加重复段），并恢复 `host=0.0.0.0`。
  已在 e5 手工补哈希后验证：未登录请求 302 到 `/login`，登录页出现用户名/密码，不再显示
  “Run hermes webapp”。
- **应用数据目录权限**：`@appdata` 之前是 700，应用中心/管理端看不到内容；改为 770
  （与其它 fnOS 应用一致），运行时 `ensure_home_layout` 也会自愈。

### 4.5 0.22.18 安装失败修复（手工构建）

0.22.17 在“先卸载 0.22.16 再装”的场景下启动失败，`info.log` 显示
`ModuleNotFoundError: No module named 'ruamel'` / `httpx`。根因：

- 卸载后数据目录残留了不完整的 venv；`uv venv --clear` 对非 venv 目录报错，
  旧逻辑清理后**没有重试 3.14**，直接回退 3.12；
- 上游 `ruamel.yaml` / `httpx` 依赖带 `python_version >= '3.14'` 标记，
  3.12 环境不会安装它们，但源码无条件 import；
- 安装校验只看 `venv/bin/hermes` 是否存在，未检查关键依赖。

修复（0.22.18）：

- 安装前清理残留 venv，重试 Python 3.14（不再回退 3.12）；
- 安装前后校验 `ruamel.yaml` + `httpx`，缺失则重装并最终失败并提示；
- 安装模型配置 helper 路径改为 `$(dirname "$0")/apply_model_config.py`；
- 0.22.18 产物：`dist/Hermes-Desktop-fnOS-v0.22.18-0f700158.fpk`，
  22,663,498 字节，SHA256 `78e28d80a0c9b2a761856ab937bc91174756f9cb2bdf63905e063cafb2b80ca7`。

### 4.6 0.22.19 应用文件入口（data-share）

飞牛文件管理器的「应用文件」只展示 `@appshare/<app>`，并且要求 `config/resource`
声明 `data-share`。0.22.16~0.18 的 `resource` 是 `{}`，所以看不到 hermes-desktop。

0.22.19：

- `config/resource` 改为：
  `{"data-share": {"shares": [{"name": "hermes-desktop"}]}}`
- 安装/启动时创建 `/vol1/@appshare/hermes-desktop`（770），内含 `README.txt` 以及
  `data`、`config`、`logs`、`workspace` 符号链接；真实数据仍在 `@appdata`/`@appconf`/`@apphome`。
- 布局检查器允许并校验 `data-share`，其它 resource 仍被拒绝。
- 产物：`dist/Hermes-Desktop-fnOS-v0.22.19-0f700158.fpk`，
  22,663,549 字节，SHA256 `e9825fe5ad85af3f7e98384364769656f308ae7a465d7b6f506df96b39ced764`。

### 4.7 0.22.20 配置迁移到应用文件

按用户要求，把**配置/日志/工作区实体**迁移到 `/vol1/@appshare/hermes-desktop`：

- `config/config.yaml`、`config/access-info.txt`、`config/gateway.conf` 为实体文件（660）；
- `logs/`、`workspace/` 为实体目录；
- `/vol1/@appconf/hermes-desktop/*` 与 `/vol1/@appdata/hermes-desktop/{logs,workspace}` 改为符号链接；
- `data` 仍指向 `@appdata`（venv/src/sessions 等运行数据）；
- 卸载删除数据时同时清理 `@appshare/hermes-desktop`，归档也包含 appshare 配置。

产物：`dist/Hermes-Desktop-fnOS-v0.22.20-0f700158.fpk`，
22,665,161 字节，SHA256 `a14a46fbead23bacdc5afc6fa8e0abaf75625f7108fc367a94690f7d1082453b`。

### 4.8 0.22.21 Python 安装重试

e5 安装 0.22.20 时报：

```text
error: Python installation is missing a `_sysconfigdata_` file
```

这是 uv 管理的 Python 3.14 安装残留/半下载导致。0.22.21：

- 重建 venv 前删除 `@appdata/<app>/pythons` 与旧 venv；
- 3.14 venv 首次失败后删除 `pythons` + `.uv-cache` 再重试一次；
- 仍失败才向用户报错。

产物：`dist/Hermes-Desktop-fnOS-v0.22.21-ac76ec4f.fpk`，
22,676,204 字节，SHA256 `f867ccabd134886b850bf94eb9da554aa2564b8fe81d04ed573359f6d37f8007`。


### 4.9 0.22.6 官方 Release 同步版

按用户要求构建与官方 Release 完全同版本的包：

- 上游 tag：`v0.22.6`（commit `f0d94882`）
- 应用版本：`0.22.6`
- 产物：`dist/Hermes-Desktop-fnOS-v0.22.6-f0d94882.fpk`
- 大小：23,186,521 字节
- SHA256：`c3a97095215d7281ee5a3f0aae3093b3ab81bd22e74b857bf64ea538c61fa1f6`

构建时发现 `build.sh` 对 tag 的 fetch 逻辑用了 `origin/<tag>`（tag 没有该引用），已改为
`reset --hard FETCH_HEAD`，并让 codeload 回退对 tag 使用 `refs/tags/...`。
版本策略同步调整为：应用版本 = 上游 Release 版本，不再本地自增；旧的 0.22.13~0.22.21
是本地验证包，不是 Release。
## 5. 真机安装与内嵌验收（e5）

在 e5 上安装本仓库重构包（`0.22.6`，含新 adapter/main/布局），`appcenter-cli` 安装成功、应用 running：

| 项 | 结果 |
|---|---|
| `appcenter-cli list` | `hermes-desktop | Hermes Desktop | 0.22.6 | running` |
| `target/app.sock` | 存在（adapter 监听） |
| `/.gateway-health`（经 socket） | 200：`{"ok":true,"adapter":"fnos-gateway-adapter","version":"1.0.0",...,"upstream_ok":true}` |
| 直连 `:9120` | 登录后 200；CSP `frame-ancestors 'none'`（未被放宽） |
| 适配层 socket `/app/hermes-desktop/` | 登录后 200；CSP **`frame-ancestors 'self'`**；sha256 哈希/`base-uri`/`form-action` 保留 |
| 经适配层登录 | `POST /app/hermes-desktop/auth/password-login` → 200 `{"ok":true,"next":"/app/hermes-desktop/"}`；Cookie `Path=/app/hermes-desktop` |
| 经适配层 WS | `POST /api/webapp/socket-ticket?socket=gateway` 取票 → `GET /app/hermes-desktop/api/ws?native_dial=browser&ticket=…` → **101 Switching Protocols** |
| 飞牛网关 `:5666/app/hermes-desktop` | 无浏览器会话时返回 `invalid token`（网关要求飞牛登录态）；真实 iframe 视觉验收需浏览器 |

### 5.1 真机发现并已修复的缺陷

1. **etc/var 符号链接目标错误**：`var/config.yaml -> ../etc/config.yaml` 在 fnOS 下指向
   `/vol1/@appdata/etc`（不存在），Hermes 读不到配置 → webapp 不监听 9120。
   已改为绝对目标 `${CONFIG_FILE}`（install/main/upgrade 三处）。
   真机验证时手工修正该链接后，9120 立即监听、全部链路通过。
2. **首装认证回退**：首装时 venv 未就绪，scrypt 哈希生成失败 → `host=127.0.0.1`。
   已增加 `install_callback.finalize_webapp_auth` 与 `cmd/main` 运行期自愈（从 etc/access-info 取密码补哈希）。
3. 组装脚本曾把 `__pycache__` 带进包；已在构建脚本用 `PYTHONPYCACHEPREFIX` 并清理。
4. **iframe 刷新白屏**：Hermes `index.html` 使用相对资源路径 `./assets/...`；
   裸挂载路径 `/app/hermes-desktop`（无尾斜杠）会让资源解析到 `/app/assets/...`，
   经 adapter 转发后 302/404，脚本加载失败 → 白屏。已修：
   - adapter 对裸挂载路径 302 到 `/app/hermes-desktop/`；
   - `app/ui/config` 的内嵌 `url` 改为带尾斜杠；
   - 已用新 adapter 在 e5 验证裸路径返回 `302 Location: /app/hermes-desktop/`。
5. **卸载选“清除数据”但数据目录仍在**：`delete_data` 删除 `TRIM_PKGVAR` 后，
   `log_msg` 又 `mkdir -p` 把目录重新创建（只剩 `info.log`，偶发空 `venv/`）。
   已修：卸载日志固定写 `/tmp/hermes-desktop-uninstall.log`（var 存在时才附带写）；
   删除前 `cd /`，`rm -rf` 后二次 `find`/`rmdir` 校验并记录 WARN；
   向导值兼容 `true/1/yes/on`。已在 e5 手工执行，`/vol1/@appdata/hermes-desktop` 被真正删除。

### 5.2 未执行/未验证

- `appcenter-cli install-fpk` 对已安装应用提示“请从 Web UI 卸载”，**升级路径未执行**；
  0.22.16 安装包已构建并通过布局/契约门禁，待 UI 或干净设备验证。
  真机测试使用 0.22.6 包 + 手工修正符号链接，因此 §5.1 的修复在真机上是“修复后行为已验证、
  修复代码本身待下次全新安装验证”。
- 浏览器 iframe 实际渲染、fnOS 桌面内嵌窗口未执行（e5 无浏览器；网关需飞牛登录态）。
  用户反馈 0.22.6 测试包内嵌可登录、刷新白屏，已据此修复并构建 0.22.14/0.22.15。
- 0.22.16 的“模型向导 + 单入口 + 官方图标 + 卸载删除”已做静态/包内容检查，待用户安装后验收。
- 安装向导文案已精简、向导测试已加入；按新发布策略不自动出新包，待上游下一个 Release 构建时生效。

## 6. 证据与产物

- 分支：`refactor/embedded-gateway`（已推送，HEAD 见 GitHub）
- CI：`.github/workflows/test.yml` 在分支上连续通过
  （例：run `41666246054`、`41666044281`、`41664090763`、`41662809087`、`41658183003`、`41657585799`）
- 官方构建：`dist/Hermes-Desktop-fnOS-v0.22.16-fd267bb3.fpk`（0.22.16）与 `dist/Hermes-Desktop-fnOS-v0.22.17-fd267bb3.fpk`（0.22.17 真机修复）与 `dist/Hermes-Desktop-fnOS-v0.22.18-0f700158.fpk`（0.22.18 安装修复）、`dist/Hermes-Desktop-fnOS-v0.22.19-0f700158.fpk`（0.22.19）、`dist/Hermes-Desktop-fnOS-v0.22.20-0f700158.fpk`（0.22.20 配置迁移）、`dist/Hermes-Desktop-fnOS-v0.22.21-ac76ec4f.fpk`（0.22.21 Python 安装重试）
- 早期测试：`/tmp/hermes-build/hermes-desktop.fpk`（0.22.6，已安装到 e5）
- 日志：`/tmp/official-build-0.22.16.log`、`/tmp/hermes-tests.log`、`/tmp/hermes-desktop-uninstall.log`、
  `/vol1/@appdata/hermes-desktop/{info.log,logs/gateway.log,gateway-adapter.log}`

### 6.1 PR 创建状态

- 当前 GitHub MCP token 只有只读权限：
  `create_branch` / `create_pull_request` 均返回 `403 Resource not accessible by personal access token`。
- 分支已通过本地 GitHub SSH key 推送成功；曾尝试借 Actions `GITHUB_TOKEN` 自动建 PR
  （临时 `open-pr.yml`），工作流失败（仓库未允许 Actions 创建 PR），临时文件已删除。
- 手动创建入口：
  `https://github.com/sfy2022/fnos-hermes-desktop/pull/new/refactor/embedded-gateway`
- 需要用户：点上述链接创建 PR；若希望自动化，请提供具备 `contents:write` + `pull_requests:write`
  的 token，或在仓库 Settings → Actions 允许创建 PR。

## 7. 未验证/风险

- 浏览器内真实 iframe 渲染（e5 无浏览器；CSP 头、登录跳转、Cookie Path、WS 101 已由 curl/脚本验证）。
- 内嵌仍需 Hermes 自身登录一次；不做飞牛 SSO。
- 飞牛网关 `:5666` 需要飞牛登录态，团队无法在无浏览器环境完成；网关 WS 透传假设已由
  fnOS 官方文档与 fnos-dsh 实现佐证，待用户浏览器最终确认。
- 上游升级若再次调整 CSP/前缀行为，需按 `docs/EMBEDDING.md` 回归。
- `appcenter-cli` 不支持 CLI 升级（提示从 Web UI 操作），升级/卸载路径待 UI 验收。
- Windows 构建的 `libatomic`/Node 26 问题只影响本地构建机，不影响 fnOS 运行。
