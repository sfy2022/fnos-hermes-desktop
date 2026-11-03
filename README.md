# Hermes Desktop for fnOS

飞牛 NAS（fnOS）上的 **Hermes Desktop 官方桌面工作区** —— 用官方自带的 Webapp 形态承载，
不改动任何上游代码，无需 Electron，无需虚拟显示串流。

**[⬇️ 下载最新版](https://github.com/sfy2022/fnos-hermes-desktop/releases/latest)** ·
[English](README.en.md) · [实施报告](docs/IMPLEMENTATION.md)

> 打包分发 [巴盟女婿](https://github.com/sfy2022/fnos-hermes-desktop) ·
> 上游 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)

---

## 下载安装

1. 打开 [Releases](https://github.com/sfy2022/fnos-hermes-desktop/releases/latest)，下载
   `Hermes-Desktop-fnOS-v<版本>-<commit>.fpk`（约 22 MB，x86_64）
2. 飞牛桌面 → **应用中心** → 右上角 **手动安装** → 选择该 fpk
3. 安装向导会让你设置 **访问用户名 / 密码**；密码留空则自动生成 20 位随机密码，
   可在应用数据目录的 `access-info.txt` 查看
4. **模型配置**：向导里可直接选 OpenRouter / Anthropic / DeepSeek / 自定义 OpenAI 兼容服务，
   填 API Key 和模型名（也可选「稍后配置」）。Key 只保存在 NAS 本机，
   `config.yaml` / `.env` 权限 0600
5. 装完桌面**只有一个图标**，名称为 **Hermes**（桌面内嵌入口）。
   浏览器直连不建图标：直接访问 `http://<NAS_IP>:9120`（应用介绍里也会提示该端口）。

> **应用数据位置**（飞牛官方目录规范）：
> - 应用文件（文件管理 → 应用文件，用户可见）：`/vol1/@appshare/hermes-desktop`
> - 数据（会话、venv、日志）：`/vol1/@appdata/hermes-desktop`
> - 配置（`config.yaml`、`access-info.txt`）：`/vol1/@appconf/hermes-desktop`
> - HOME：`/vol1/@apphome/hermes-desktop`
>
> 「应用文件」里的 `data`/`config`/`logs`/`workspace` 是指向真实目录的入口；
> 真实数据仍在 fnOS 标准目录，避免把密钥/运行环境直接暴露到共享目录。

> 下载慢或失败多半是国内访问 GitHub 大文件 CDN 的问题，挂代理更稳。

---

## 桌面内嵌 + 浏览器直连

桌面只有一个入口 **Hermes**（飞牛统一网关 `/app/hermes-desktop`）；
浏览器直连不占桌面图标，手动访问 `http://<NAS_IP>:9120` 即可。两者访问**同一个后端、同一份会话**：

| 方式 | 地址 | 鉴权 | 适用场景 |
|---|---|---|---|
| **桌面内嵌（唯一图标）** | 飞牛统一网关 `/app/hermes-desktop` | 飞牛登录态 + Hermes 自身登录 | 想在飞牛窗口内使用 |
| **浏览器直连（无图标）** | `http://<NAS_IP>:9120` | Hermes 自身 `basic_auth`，登录一次后浏览器记住 | 手机、平板、其他电脑 |

**内嵌如何工作**：飞牛网关把 `/app/hermes-desktop/*` 转发到应用 `target/app.sock`，
由薄适配层 `app/runtime/gateway/gateway_adapter.py` 剥前缀、注入 `X-Forwarded-Prefix`
并透传 WebSocket → `127.0.0.1:9120`。上游据此处理资源 URL、登录跳转与 Cookie Path。
唯一的安全头调整是把页面 CSP 的 `frame-ancestors 'none'` 放宽为 `'self'`（仅网关路径；
直连 `:9120` 仍保持 `'none'`），响应体不做任何改写。细节与排障见
[docs/EMBEDDING.md](docs/EMBEDDING.md)。

---

## 界面预览

| 飞牛应用中心 | 消息平台 |
|---|---|
| ![应用中心](docs/screenshots/app-center.jpg) | ![消息平台](docs/screenshots/messaging.png) |

左：在应用中心里就是一个普通应用，版本与来源一目了然。
右：内置消息平台 —— Telegram / Discord / Slack / Mattermost / Matrix / WhatsApp / Signal /
BlueBubbles / Email / SMS / 钉钉 / 飞书 / Google Chat / 企业微信 / 微信 / QQ Bot / 元宝，
填好 Bot 令牌即可让 Agent 在聊天里干活。

---

## 这是什么

Hermes 官方桌面端（Electron）的渲染层有两套产物：

| 产物 | 用途 | 本项目 |
|---|---|---|
| Electron 包（AppImage/deb/rpm） | 桌面客户端 | 官方 Linux 发行腿已禁用，NAS 也无显示环境 |
| **Webapp bundle**（`dist-webapp`） | **浏览器里的 Desktop 工作区** | ✅ 采用 |

上游 `hermes_cli/webapp.py` 的设计意图是：浏览器界面与 Dashboard **共用同一套传输层、
同一套会话与工具**，从不另起一套 HTTP/WebSocket 栈。本项目只做一件事 ——
**把官方构建产物打包成 fnOS 应用**，所以在 NAS 上看到的是**原版 Desktop 界面**，不是仿制品。

---

## 首次使用

1. 点图标打开，用安装时设置的账号登录
2. 进入**模型接入向导**：填自己的 API Key，或先点「稍后选择」
3. 之后即可新建会话对话；`hermes doctor` 可随时体检

---

## fnOS 授权面板

应用自带「fnOS 授权」面板，用来给 Agent 开通本机或其它 fnOS 设备的操作权限
（文件、Docker、应用中心、存储、监控等）。

入口在**右下角状态栏**：`● fnOS 1/2` —— 已授权 / 总数，琥珀点表示还有未授权，点击进入。
也可直接访问 `http://<NAS_IP>:9120/#/fnos-auth`。

**用法**

1. 面板顶部填 NAS 地址（`192.168.1.28` 或 `nas.example.com:5666`）→ **添加**
2. 点该设备的 **发起授权** → 得到一条登录链接
3. 浏览器打开链接，用飞牛账号登录并确认授权
4. 复制页面给出的授权码 → 粘回面板 → **提交**

> **一次只用一个链接。** 每次点「发起授权 / 重新授权」都会生成新的 client_id，
> 上一个立即失效。面板加了 2.5 秒冷却避免误连点。

面板背后是同一套 API，Agent 也能直接调用：`fnos_auth_devices`、`fnos_auth_add`、
`fnos_auth_start`、`fnos_auth_submit`、`fnos_auth_check`、`fnos_auth_logout`。
在会话里说「给 E5 授权」即可。

> 入口没放在左侧导航，是因为这个前端版本的插件贡献区只有
> `layouts / panes / routes / statusBar.* / titleBar.*`，没有 nav / sidebar ——
> `routes` 只注册路由、不生成导航项。

---

## 应用更新

右下角状态栏常驻显示当前版本（如 `v0.22.9`）。有新版本时变成琥珀色的
**可更新 0.22.10**，点进去能看到更新说明、下载并安装。

升级是**手动三步**，不会静默重启把你正在跑的会话掐掉：

1. **下载更新** —— 先走 GitHub 官方地址，失败自动换国内加速前缀；下完用 release
   里的 `build-info.json` 校验 sha256，不匹配就丢弃
2. **立即安装并重启** —— 交给 `trim-cli app install-fpk` 走 fnOS 原生升级流程，
   `upgrade_callback` 会重新拉上游源码、重装依赖，**用户数据与配置保留**
3. 安装期间应用会重启，页面断开属正常，约 2–5 分钟

> 安装依赖 `trim-cli` 已授权（就是「fnOS 授权」面板给本机授权那一步）。
> 未授权时按钮会禁用，可点「在 GitHub 上查看」手动下载 fpk。

---

## 功能

与官方 Desktop Webapp 一致：

- **对话**：流式响应、实时工具活动、结构化工具调用摘要
- **并排预览**：网页 / 文件 / 工具输出同屏
- **文件浏览**、**Artifacts** 产出物画廊
- **定时任务**、**多 Profile**、**技能管理**
- **终端栏**、**Git 审阅**
- **消息网关**：微信 / QQ / 飞书 / Telegram / Discord / Slack 等
- **MCP 集成**、**持久记忆**
- **内置飞牛官方 Skill**：装完即可操控本机 fnOS

与原生 CLI / Desktop / 网关共享同一会话与队列。

### 内置飞牛 Skill

应用内置**飞牛官方 fnOS Skill**（trim-cli + 知识文档），装完 Agent 就具备操作本机 NAS 的能力：
文件管理、Docker 控制、应用中心（查询/安装/启停/卸载）、存储与 RAID、相册与媒体库、
日志与监控、网络与电源、用户与权限、下载任务。

需要一次 OAuth 授权（trim-cli 用 Authorization Code + PKCE，Agent 不能代你确认）：

```bash
/var/apps/hermes-desktop/var/skills/fnos-trim/scripts/trim-cli login --no-open
```

命令会打印 `http://<NAS_IP>:5666/signin?...` 链接，浏览器打开确认授权，把返回的授权码
粘回命令提示即可。授权状态可用 `cmd/main setup-fnos` 查看；token 存在应用数据目录
（`~/.config/trim-cli/`），卸载保留、重装免重新授权。

> ⚠️ 授权后 Agent 等同于拥有你的 fnOS 账号权限。请遵循最小权限原则，只勾选真正需要的模块。

### 相对 Electron 少了什么

- 全局快捷键、置顶浮窗、外部终端唤起
- `safeStorage` 凭据加密存储
- 麦克风 / 语音、系统通知、剪贴板读取（HTTP 非安全上下文下浏览器禁用）

需要这些能力时，在自己的电脑上装原生 Desktop，用 **remote mode** 连同一个 gateway 即可。

---

## 安全

⚠️ **登录后的用户等同于授予本机命令执行权限**（能跑 terminal 工具、读写文件、访问凭据）。

- 本应用绑定 `0.0.0.0:9120`，因此 **Hermes 强制要求配置认证**。上游 `should_require_auth()`
  只看绑定地址，非回环即启用认证门；若 `config.yaml` 缺少 `dashboard.basic_auth`，
  Hermes 会**拒绝启动 webapp** —— gateway 主循环照跑、进程活着、端口却不监听，
  外部表现就是「打不开 / 502」。
- **切勿直接暴露到公网。** 需要外网访问请用 VPN / Tailscale，或反代 + HTTPS。
- 改密码时 `dashboard.basic_auth.password_hash` 要求 **scrypt 哈希，不能写明文**：

  ```bash
  cd /vol1/@appdata/hermes-desktop
  HERMES_PW='你的新密码' PYTHONPATH=src venv/bin/python3 -c \
    'import os; from plugins.dashboard_auth.basic import hash_password; \
     print(hash_password(os.environ["HERMES_PW"]))'
  ```

  把输出写进 `config.yaml` 的 `password_hash`，然后重启应用。

---

## 构建

### 自动构建（只跟随上游 Release）

**只有上游 `NousResearch/hermes-agent` 发布新 Release 时才构建**，不再跟随 main 的每次提交，
也不再每日无条件构建。打包脚本改动只跑 `test.yml`，不会自动发版。

| 工作流 | 触发 | 作用 |
|---|---|---|
| `sync.yml` | 每 6 小时 / 手动 | 查上游最新 Release tag，与 `.upstream-release` 不同才触发构建 |
| `build.yml` | `sync.yml` 触发 / 手动 | 按该 **release tag** 构建（不跟 main）→ 打包 fpk → 发 Release → 记录 `.upstream-release` |

应用版本号与上游 Release 无关（上游 0.22.6、本地可能已到 0.22.17）：构建时取「上游 Release 版本」
与「本地已有 tag」中较大者，再跳过已占用号，保证 fnOS 能升级。需要重建已构建过的 Release 时，
手动运行 `build.yml`，勾选 `force` 或指定 `upstream_ref`。

### 本地构建

需要 **Node >= 26**（上游 build 脚本的硬性要求）与 git/curl：

```bash
bash scripts/build.sh
# 产物：dist/hermes-desktop.fpk + dist/build-info.json
```

可用环境变量：

```bash
UPSTREAM_REF=v0.22.6   # 建议用上游 release tag；默认 main 仅用于开发验证
APP_VERSION=0.23.0     # 覆盖 manifest 版本号（同版本 fnOS 装不上）
SKIP_BUNDLE=1          # 复用已有渲染层
OUTPUT_DIR=/path/out   # 指定输出目录
```

---

## 常见问题

### Releases 里没有 fpk 下载

Actions 构建**全绿**但 Releases 只有 `Source code (zip/tar.gz)`，通常是发布步骤失败
（`continue-on-error` 会把失败步骤显示成绿色）。检查那次运行的日志，常见两类：

- **附件上传失败**：GitHub 的 Release 附件必须传到 `uploads.github.com`，与创建 Release 的
  `api.github.com` 不是同一个 host
- **权限不足**：仓库 `Settings → Actions → General → Workflow permissions` 选
  **Read and write permissions**

临时替代：Actions 页面底部 **Artifacts** 里的 `fnos-hermes-desktop-fpk`（需登录，保留 30 天）。

### 点「启动」报错 / 打不开 / 502

**先看 Hermes 自己的日志**，而不是安装脚本的 info.log：

```bash
tail -50 /vol1/@appdata/hermes-desktop/logs/gateway.log
```

`info.log` 往往只有一行启动横幅 —— 进程可能在**日志初始化之前**就退出了。

| 现象 | 原因 | 处理 |
|---|---|---|
| `Refusing to bind dashboard to 0.0.0.0` | 绑了 `0.0.0.0` 但缺 `dashboard.basic_auth` | 补上 `basic_auth`（见「安全」），或改绑 `127.0.0.1` |
| 启动横幅一闪而过、无任何报错 | 数据目录混入**非应用属主**的文件 | 见下条 |

### 数据目录混入其他用户属主的文件

Hermes 以 `hermes-desktop` 身份运行。若用其他账号（比如 SSH 登录的管理员）手工跑过 `hermes`，
会留下别的属主的文件 —— `webapp/session-token`、`gateway.lock`、`tools/` 等。
应用身份读写不了，**会在日志初始化之前静默退出**，日志里一行错都没有。

```bash
find /vol1/@appdata/hermes-desktop ! -user hermes-desktop
```

**教训：不要用别的账号手动跑 Hermes。** 排障请用 Web UI 的终端栏（那里是应用身份）。

### 内嵌窗口提示 `Desktop IPC bridge is unavailable`

WebSocket 没通。先看适配层日志与健康端点：

```bash
tail -50 /vol1/@appdata/hermes-desktop/logs/gateway-adapter.log
curl --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/.gateway-health
```

确认 `cmd/main` 已启动 adapter、`gateway.conf` 的 prefix/socket 与
`app/ui/config` 一致，且飞牛网关透传 `Upgrade: websocket`。仍失败时用浏览器直连
`http://<NAS_IP>:9120`（不经过网关），或在 `cmd/main diagnose` 输出中看两级 CSP 对照。

### 内嵌登录后跳到飞牛桌面

上游用 `window.__HERMES_BASE_PATH__` 拼路由，并由 `X-Forwarded-Prefix` 注入网关前缀。
若跳到飞牛桌面，检查适配层是否发送了 `X-Forwarded-Prefix`、页面里该变量是否为
`/app/hermes-desktop`，以及是否误启用了旧的 HTML 改写/桥接脚本（新版已删除）。

### 安装时报 `A directory already exists at .../venv`

卸载时选了「保留数据」或卸载中断，`venv` 残留。新版本已用 `uv venv --clear` 并加了残留兜底；
旧包可手动 `rm -rf /vol1/@appdata/hermes-desktop/venv` 后重装。

---

## 目录结构

```
.
├── .github/workflows/
│   ├── build.yml              # 构建 + 发布
│   └── sync.yml               # 上游变更检测
├── scripts/
│   └── build.sh               # 完整构建流程
├── packaging/fpk/             # fpk 模板（渲染层在构建时注入）
│   ├── manifest
│   ├── cmd/                   # 生命周期脚本
│   ├── config/                # privilege / resource
│   ├── wizard/                # 安装向导（JSON）
│   ├── app/plugins/           # 内置插件（fnOS 授权面板）
│   └── app/ui/config          # 桌面入口
└── docs/IMPLEMENTATION.md     # 实施报告（含完整踩坑记录）
```

构建与运行期的坑（Node ≥ 26、Python 3.14、uv 的 `exclude-newer`、fnOS `wizard` 必须是
JSON 数组、插件三套加载机制等）都记在 [实施报告](docs/IMPLEMENTATION.md) 里。

---

## 相关链接

- 上游：<https://github.com/NousResearch/hermes-agent>
- 官方 Desktop 文档：<https://hermes-agent.nousresearch.com/docs/user-guide/desktop>
- fnOS 开发者文档：<https://developer.fnnas.com/>
- fnpack：<https://developer.fnnas.com/docs/cli/fnpack>

## 许可

本仓库（fpk 打包与自动化流水线部分）为社区贡献的开源项目。
上游 Hermes Agent 遵循其自身许可（MIT）。
