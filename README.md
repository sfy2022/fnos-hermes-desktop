# Hermes Desktop for fnOS

飞牛 NAS（fnOS）上的 **Hermes Desktop 官方桌面工作区** —— 用官方自带的 Webapp 形态承载，
**不改动任何上游代码**，无需 Electron，无需虚拟显示串流。

[English](README.en.md) · [实施报告](docs/IMPLEMENTATION.md)

---

## 这是什么

Hermes 官方桌面端（Electron）的渲染层有两套构建产物：

| 产物 | 用途 | 本项目 |
|---|---|---|
| Electron 包（AppImage/deb/rpm） | 桌面客户端 | 官方 Linux 发行腿已禁用，NAS 也无显示环境 |
| **Webapp bundle**（`dist-webapp`） | **浏览器里的 Desktop 工作区** | ✅ 采用 |

上游 `hermes_cli/webapp.py` 的注释说明了设计意图：

> *Build and launch plumbing for the browser-hosted **Desktop workspace**. The browser surface
> **shares Hermes' hardened dashboard server**... it never creates a second HTTP/WebSocket stack.*

也就是说，官方 Desktop 的 UI 与浏览器 Dashboard **共用同一套传输层、同一套会话与工具**。
本项目做的是：**把官方构建产物打包成 fnOS 应用**，让 NAS 桌面上点图标就能用。

**结果**：你在 NAS 上看到的是**原版 Desktop 界面**，不是仿制品。

---

## 功能

与官方 Desktop Webapp 一致（官方文档 `user-guide/desktop.md`）：

- **对话**：流式响应、实时工具活动、结构化工具调用摘要
- **并排预览**：网页 / 文件 / 工具输出同屏
- **文件浏览**：直接浏览工作目录
- **Artifacts**：会话产出物画廊
- **定时任务**、**多 Profile**、**技能管理**
- **终端栏**、**Git 审阅**
- **消息网关**：微信 / QQ / 飞书 / Telegram / Discord / Slack 等
- **MCP 集成**、**持久记忆**
- **内置飞牛官方 Skill**：装完即可操控本机 fnOS（文件、Docker、应用中心、存储、监控…）

与原生 CLI / Desktop / 网关共享同一会话与队列。

### 内置飞牛 Skill

本应用内置**飞牛官方 fnOS Skill**（trim-cli + 知识文档），装完 Agent 就具备操作
本机 NAS 的能力：文件管理、Docker 控制、应用中心（查询/安装/启停/卸载）、
存储与 RAID、相册与媒体库、日志与监控、网络与电源、用户与权限、下载任务。

**需要一次 OAuth 授权**（trim-cli 使用 Authorization Code + PKCE，Agent 不能代你确认）：

```bash
/var/apps/hermes-desktop/var/skills/fnos-trim/scripts/trim-cli login --no-open
```

命令会打印一个 `http://<NAS_IP>:5666/signin?...` 链接，在浏览器打开并确认授权，
把返回的授权码粘贴回命令提示即可。授权状态可用
`cmd/main setup-fnos` 查看。

授权 token 存在应用数据目录（`~/.config/trim-cli/`），卸载保留、重装免重新授权。

> ⚠️ 授权后 Agent 等同于拥有你的 fnOS 账号权限。请遵循最小权限原则，
> 只勾选真正需要的模块（scope 按模块申请）。

### 相对 Electron 少了什么（浏览器形态的固有限制）

- 全局快捷键、置顶浮窗、外部终端唤起
- `safeStorage` 凭据加密存储
- 麦克风 / 语音、系统通知、剪贴板读取（HTTP 非安全上下文下浏览器禁用）

需要这些能力时，在你的 Windows/macOS 上装原生 Desktop，用 **remote mode** 连同一个 gateway 即可。

---

## 安装

飞牛桌面 → **应用中心** → 右上角 **手动安装** → 选择 `hermes-desktop.fpk`

安装向导会询问：

1. **访问用户名 / 密码**（密码留空则自动生成 20 位随机强密码，可在应用数据目录 `access-info.txt` 查看）
2. **GitHub 下载通道**（默认自动探测）与 **Python 依赖源**（默认 PyPI 官方）

安装完成后：

1. 桌面出现 **Hermes Desktop** 图标，点击在飞牛窗口内打开
2. 首次进入会要求配置模型供应商（也可在向导阶段先填 API Key）
3. 想让 Agent 操控 NAS，把[官方 fnOS Skill](https://club.fnnas.com/forum.php?mod=viewthread&tid=67918)喂给它

---

## 安全

⚠️ **登录后的用户等同于授予本机命令执行权限**（能跑 terminal 工具、读写文件、访问凭据）。

- 本应用默认绑定 `0.0.0.0:9120` 并**强制密码认证**（`host: 0.0.0.0` 会触发 dashboard 登录门）
- **切勿直接暴露到公网**。需要外网访问请用 VPN / Tailscale，或反代 + HTTPS
- 改密码：编辑应用数据目录的 `config.yaml` 中 `dashboard.basic_auth`，然后重启应用

---

## 自动构建

本仓库用 GitHub Actions 自动同步上游并产出 fpk：

| 工作流 | 触发 | 作用 |
|---|---|---|
| `sync.yml` | 每 6 小时 | 对比上游 commit，有变化才触发构建 |
| `build.yml` | 每日 03:17 / 手动 / 脚本变更 | 克隆上游 → 构建渲染层 → 打包 fpk → 发 Release |

构建产物在 **Releases** 页，直接下载 `hermes-desktop.fpk` 安装。

### 本地构建

需要 **Node >= 26**（上游 build 脚本的硬性要求）与 git/curl：

```bash
bash scripts/build.sh
# 产物：dist/hermes-desktop.fpk + dist/build-info.json
```

可用环境变量：

```bash
UPSTREAM_REF=v0.22.5   # 构建指定上游版本
SKIP_BUNDLE=1          # 复用已有渲染层
OUTPUT_DIR=/path/out   # 指定输出目录
```

---

## 技术要点（踩坑记录）

这些是实机验证中踩出来的，都已固化进 `scripts/build.sh` 与 `packaging/fpk/cmd/`。

### 构建侧

- **Node 必须 >= 26**：上游 `scripts/build/node-deps.mjs` 硬性检查，Node 22 会被拒
- **渲染层构建要跳过 Electron 二进制**：`build-webapp.mjs` 已内置处理（`electron: false`）
- **渲染层不提交进仓库**：47MB / 1081 文件，每次构建重新生成

### 运行时侧（fpk 安装脚本）

1. **必须装 GitHub 源码版**：PyPI 上 `hermes-agent` 最高 0.19.0，而该版本**没有 `hermes webapp` 命令**
2. **必须 Python 3.14**：依赖按 `python_version >= '3.14'` 声明（`ruamel.yaml`），但代码无条件 import
3. **依赖解析必须用官方 PyPI 源** + `--exclude-newer 2027-01-01`：
   uv 0.13+ 默认带 `exclude-newer`（其构建日），而**国内镜像不携带 upload-time 元数据**，
   导致依赖被全部误判"过新"而过滤
4. **GitHub 多路备援**：git clone → codeload → gh-proxy.com → ghproxy.net → 直连；
   NAS 上 `curl` 必须加 `--http1.1`
5. **uv 下载不能拼 URL**：wheel 文件名带版本号且各源命名不同，改用 PyPI JSON API 定位
6. **`TRIM_SERVICE_PORT` 可能为空**：需兜底到 9120 并做数字校验
7. **fnOS `wizard/*` 必须是 JSON 数组**：写成 shell 会导致 `fnpack build` 直接报错

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
│   └── app/ui/config          # 桌面入口（iframe → 9120）
└── docs/IMPLEMENTATION.md     # 实施报告
```

---

## 相关链接

- 上游：<https://github.com/NousResearch/hermes-agent>
- 官方 Desktop 文档：<https://hermes-agent.nousresearch.com/docs/user-guide/desktop>
- fnOS 开发者文档：<https://developer.fnnas.com/>
- fnpack：<https://developer.fnnas.com/docs/cli/fnpack>

## 许可

本仓库（fpk 打包与自动化流水线部分）为社区贡献的开源项目。
上游 Hermes Agent 遵循其自身许可（MIT）。

---

## 在 Gitea 上运行 CI

本仓库的工作流**同时兼容 GitHub Actions 与 Gitea Actions**（已在你自建的
Gitea `<gitea-host>` 上验证被正确识别）。

### 已做的兼容处理

| 差异点 | 处理方式 |
|---|---|
| Release API | 抽到 `scripts/publish_release.py`，按 `GITHUB_API_URL` 自动适配（两平台路径一致） |
| `actions/github-script`（GitHub 专属） | 移除，改用 curl 调 Gitea 的 dispatch API |
| `actions/upload-artifact@v4` | 降级到 `v3`（Gitea 只支持到 v3，GitHub 同样接受） |
| `actions/setup-node` | 改为直接下载 Node 26 二进制，避免 action 源差异与版本清单滞后 |
| 触发方式 | Gitea 走 dispatch API，GitHub 走 `gh` CLI |

### 启用 Gitea Actions

Gitea 需要 **runner** 才能执行工作流。步骤：

1. 在 Gitea 打开仓库 → **Settings** → **Actions** → **Runners** → **New runner**
2. 选择 **docker** 或 **host** 模式，复制注册命令
3. 在你的构建机上执行（用 `act_runner` 官方镜像）：

```bash
docker run -d \
  --name gitea-runner \
  --restart always \
  -e GITEA_INSTANCE_URL="https://<gitea-host>" \
  -e GITEA_RUNNER_REGISTRATION_TOKEN="<上一步复制的 token>" \
  -e GITEA_RUNNER_NAME="fnos-builder" \
  -e GITEA_RUNNER_LABELS="ubuntu-latest:docker://node:26-bookworm" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  gitea/act_runner:latest
```

> 用 `node:26-bookworm` 作为任务镜像：官方 build 脚本要求 Node ≥ 26。

4. 之后仓库的 Actions 页面就能看到两个工作流：
   - **同步上游并构建**（每日 03:17 + 手动）
   - **检查上游更新**（每 6 小时，有变化才触发构建）

### 需要配置的 Secret

仓库 **Settings** → **Actions** → **Secrets**：

| 名称 | 用途 |
|---|---|
| `GITEA_TOKEN` | 发布 Release 用。在 Gitea **Settings** → **Applications** 生成，勾选 `write:repository` |

未配置 `GITEA_TOKEN` 时，构建仍会正常完成，只是跳过 Release 发布步骤
（产物可从 Actions 的 artifact 下载）。

### 手动触发

```bash
curl -H "Authorization: token <GITEA_TOKEN>" \
  -H "Content-Type: application/json" \
  -X POST "https://<gitea-host>/api/v1/repos/<owner>/<repo>/actions/workflows/build.yml/dispatches" \
  -d '{"ref":"main"}'
```
