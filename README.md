# Hermes Desktop for fnOS

飞牛 NAS（fnOS）上的 **Hermes Desktop 官方桌面工作区**。使用上游自带的 Webapp 形态承载，
不修改上游代码，不需要 Electron，也不依赖虚拟显示或串流。

**[下载最新版](https://github.com/sfy2022/fnos-hermes-desktop/releases/latest)** ·
[当前状态](docs/CURRENT-STATE.md) · [English](README.en.md)

> 上游：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
> 本仓库负责 fnOS 打包、生命周期脚本和网关适配层。

---

## 下载安装

1. 打开 [Releases](https://github.com/sfy2022/fnos-hermes-desktop/releases/latest)，下载
   `Hermes-Desktop-fnOS-v<版本>-<commit>.fpk`（约 22 MB，x86_64）。
2. 飞牛桌面 → **应用中心** → 右上角 **手动安装** → 选择该 fpk。
3. 安装向导：
   - **访问账号**：设置 Hermes 用户名和密码；密码留空会自动生成。
   - **模型配置**：选择 OpenRouter / Anthropic / DeepSeek / 自定义 OpenAI 兼容服务，或稍后配置。
   - **网络配置**：选择 GitHub 下载通道和 Python 依赖源。

### 应用文件与数据位置

| 内容 | 位置 |
|---|---|
| 应用文件（文件管理 → 应用文件） | `/vol1/@appshare/hermes-desktop` |
| 配置实体 | `/vol1/@appshare/hermes-desktop/config` |
| 日志 | `/vol1/@appshare/hermes-desktop/logs` |
| 工作区 | `/vol1/@appshare/hermes-desktop/workspace` |
| 运行数据（venv、源码、会话等） | `/vol1/@appdata/hermes-desktop` |
| HOME | `/vol1/@apphome/hermes-desktop` |

`/vol1/@appconf/hermes-desktop` 中的 `config.yaml`、`access-info.txt`、`gateway.conf`
都是指向 `@appshare` 的符号链接；修改配置请直接编辑应用文件中的实体文件。

---

## 桌面入口与直连

| 方式 | 地址 | 鉴权 | 说明 |
|---|---|---|---|
| 桌面内嵌 | 飞牛统一网关 `/app/hermes-desktop/` | 飞牛登录态 + Hermes 账号 | 桌面上唯一的 Hermes 入口 |
| 浏览器直连 | `http://<NAS_IP>:9120` | Hermes 账号 | 不经过飞牛网关，不创建第二个桌面入口 |

两者访问同一个后端、同一份会话。

**内嵌链路**：飞牛桌面 iframe → fnOS 统一网关 `/app/hermes-desktop/*` →
应用 `target/app.sock` → `gateway_adapter.py` → `127.0.0.1:9120`。

适配层负责：

- 剥掉网关前缀，注入 `X-Forwarded-Prefix`；
- 重写 `Host`、`Origin`、`Referer` 到上游地址；
- 透传 WebSocket 帧；
- 把页面 CSP 的 `frame-ancestors 'none'` 放宽为 `'self'`，
  把 `X-Frame-Options: DENY` 改为 `SAMEORIGIN`；
- HTTP 模式下去掉认证 Cookie 的 `Secure` 和 `__Secure-` 前缀，避免浏览器丢弃登录 Cookie。

响应体不做改写，直连 `:9120` 的 CSP 仍保持最严格设置。

内嵌登录使用安装时设置的 Hermes 账号，**不需要先直连 `:9120` 登录一次**。
如果内嵌登录后反复回到 `/login`，先清除浏览器中该 NAS 站点的旧 Cookie，再用无痕窗口确认。

---

## 这是什么

Hermes 官方桌面端提供 Electron 包和 Webapp bundle 两种渲染产物。本项目使用 **Webapp bundle**，
因为它可以在浏览器中运行，不需要桌面显示环境。

上游 `hermes_cli/webapp.py` 的设计目标是让浏览器界面与 Dashboard 共用同一套传输层、会话和工具。
本项目只负责把这个官方构建产物打包成 fnOS 应用，因此看到的是官方 Desktop 界面，不是仿制页面。

---

## 功能

与官方 Desktop Webapp 一致：

- 流式对话、工具调用可视化、结构化工具摘要
- 并排预览：网页、文件、工具输出
- 文件浏览、Artifacts 产出物
- 定时任务、多 Profile、技能管理
- 终端栏、Git 审阅
- 消息网关：Telegram、Discord、Slack、钉钉、飞书、微信、QQ 等
- MCP 集成、持久记忆
- 内置飞牛 Skill：文件、Docker、应用、存储、网络、监控等

与原生 Electron 版相比，浏览器版不提供系统托盘、原生通知、全局快捷键、原生文件选择器、
麦克风和部分硬件加速能力。需要这些能力时，可以运行官方原生 Desktop 并连接到同一个 gateway。

---

## fnOS 授权

应用内置「fnOS 授权」面板，用于给 Agent 开通本机或其它 fnOS 设备的操作权限。

入口在右下角状态栏，也可直接访问：

```text
http://<NAS_IP>:9120/#/fnos-auth
```

使用流程：

1. 填写目标 NAS 地址并添加设备；
2. 点「发起授权」，打开生成的登录链接；
3. 用目标 NAS 账号登录并确认；
4. 把授权码粘回面板提交。

授权后 Agent 等价于拥有该 fnOS 账号的权限，请按最小权限原则只勾选需要的模块。

---

## 应用更新

右下角状态栏显示当前版本。检测到新版本时会出现更新提示，更新流程：

1. 下载新版本 fpk，并校验 SHA256；
2. 交给 fnOS 原生升级流程安装；
3. 应用重启，用户配置和会话保留。

升级/卸载需要通过飞牛应用中心 Web UI 操作。

---

## 安全

登录后的用户等同于获得本机命令执行权限（终端、文件读写、凭据访问）。

- 应用绑定 `0.0.0.0:9120`，因此 Hermes 强制要求配置认证；缺少 `dashboard.basic_auth`
  时 webapp 会拒绝监听。
- 不要直接暴露到公网。需要外网访问时使用 VPN、Tailscale 或带 HTTPS 的反向代理。
- 修改 Hermes 密码时写入 `dashboard.basic_auth.password_hash`，值是 scrypt 哈希，不能写明文。
  修改后重启应用。

---

## 构建

只有上游发布新的官方 Release 时才自动构建：

| 工作流 | 触发 | 作用 |
|---|---|---|
| `sync.yml` | 每 6 小时 / 手动 | 查上游最新 Release；与 `.upstream-release` 不同才触发构建 |
| `build.yml` | `sync.yml` / 手动 | 按上游 Release tag 构建 fpk，发布 Release，记录构建版本 |

应用版本与上游 Release 完全一致，不本地递增。需要重建已构建版本时，手动运行
`build.yml` 并勾选 `force`，或指定 `upstream_ref`。

### 本地构建

需要 Node.js >= 26、git、curl 和 fnpack：

```bash
UPSTREAM_REF=v0.22.7 APP_VERSION=0.22.7 bash scripts/build.sh
# 产物：dist/Hermes-Desktop-fnOS-v<版本>-<commit>.fpk + dist/build-info.json
```

常用环境变量：

```bash
UPSTREAM_REF=v0.22.7   # 上游 Release tag；默认取仓库脚本的默认值
APP_VERSION=0.22.7     # 覆盖 manifest 版本号
SKIP_BUNDLE=1          # 复用已有渲染层
OUTPUT_DIR=/path/out   # 指定输出目录
```

不设置 `APP_VERSION` 时，构建脚本会使用上游 tag 对应的版本号。

---

## 常见问题

### 打不开 / 502

先看应用日志和 socket：

```bash
tail -50 /vol1/@appshare/hermes-desktop/logs/gateway.log
tail -50 /vol1/@appdata/hermes-desktop/info.log
curl --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/.gateway-health
```

确认 adapter 已启动、`target/app.sock` 存在、`gateway.conf` 的 prefix/socket 与
`app/ui/config` 一致。

### 直连 9120 不监听

通常是 `config.yaml` 缺少有效的 `dashboard.basic_auth`，或 `webapp.host` 与端口配置不正确。
检查应用文件中的 `config/config.yaml`，补齐认证后重启应用。

### 内嵌登录后反复回到 `/login`

先清除该 NAS 站点的旧 Cookie，再用无痕窗口验证。旧版登录可能留下同名但 Path 不同的
`hermes_*` Cookie，浏览器会同时发送多个值，导致服务端读到旧会话。

### 配置改了不生效

确认修改的是：

```text
/vol1/@appshare/hermes-desktop/config/config.yaml
```

而不是 `@appconf` 中的符号链接或 `@appdata` 下的旧副本。

### 安装时提示 venv 已存在

旧版本卸载不完整会留下 `venv`。删除 `/vol1/@appdata/hermes-desktop/venv` 后重新安装。

---

## 目录结构

```text
.
├── .github/workflows/          # sync / build
├── scripts/                    # 构建与检查脚本
├── packaging/fpk/              # fpk 模板
│   ├── manifest
│   ├── app/
│   │   ├── ui/config           # 桌面入口
│   │   ├── runtime/gateway/    # 网关 adapter 与 gateway.conf
│   │   ├── plugins/            # 随包插件（fnOS 授权等）
│   │   └── skills/             # 内置飞牛 Skill
│   ├── cmd/                    # 生命周期脚本
│   ├── config/                 # privilege / resource
│   └── wizard/                 # 安装、配置、升级、卸载向导
├── docs/
│   ├── CURRENT-STATE.md        # 当前版本与布局
│   ├── EMBEDDING.md            # 内嵌链路与 adapter
│   ├── APP-STRUCTURE.md        # 包结构与运行期布局
│   ├── IMPLEMENTATION.md       # 历史设计记录
│   └── REFACTOR-CONTRACT.md    # 内嵌契约
└── README.en.md
```

---

## 链接

- 上游：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- 官方 Desktop 文档：<https://hermes-agent.nousresearch.com/docs/user-guide/desktop>
- 飞牛开发者文档：<https://developer.fnnas.com/>
- fnpack：<https://developer.fnnas.com/docs/cli/fnpack>
- 问题反馈：<https://github.com/sfy2022/fnos-hermes-desktop/issues>

## 许可

本仓库的 fpk 打包与自动化部分为社区贡献。上游 Hermes Agent 遵循其自身许可（MIT）。
