# Hermes Desktop 飞牛原生版 — 实施报告

> 日期：2026-11-01 ｜ 目标：让 NAS 上像 Windows 一样使用 Hermes Desktop，且**不改一行官方代码**
> 状态：**已在飞牛 NAS 实机跑通全链路**（Debian 12 / x86_64 / 23GB 内存）

---

## 一、结论

**可行，而且不需要 Electron、不需要虚拟显示、不需要串流。**

官方 Hermes Desktop 的渲染层有**两套构建产物**：

| 产物 | 用途 | 本方案 |
|---|---|---|
| Electron 包（AppImage/deb/rpm） | 桌面客户端 | ✗ 官方 Linux 发行腿已禁用，且 NAS 无显示环境 |
| **Webapp bundle**（`dist-webapp`） | **浏览器里的 Desktop 工作区** | ✅ **采用这个** |

关键在于 `hermes_cli/webapp.py` 的注释：

> *"Build and launch plumbing for the browser-hosted **Desktop workspace**. The browser surface shares Hermes' hardened dashboard server. This module owns only the separate renderer artifact... it never creates a second HTTP/WebSocket stack."*

**官方自己就把 Desktop 渲染层做成了可在浏览器运行的形态**，并且与原生 Desktop **共用同一套 `apps/shared/` 传输层、同一套 UI 代码、同一套会话与工具**。

所以「仿照 Desktop 做飞牛的」不是仿造，是**启用官方已有的第二形态**。

---

## 二、架构

```
飞牛桌面（浏览器外壳）
   └── iframe → http://<NAS_IP>:9120/
                    │
                    ▼
            hermes gateway run
            ├── dashboard mount 伺服 app/webapp（1081 个文件的官方渲染层）
            ├── JSON-RPC / WebSocket  ← 与原生 Desktop 完全相同的协议
            └── HERMES_HOME（会话/记忆/技能/配置）
                    │
                    ▼
                 模型 API
```

- 渲染层来源：官方仓库 `apps/desktop/scripts/build-webapp.mjs` → Vite 构建 → `dist-webapp`
- **未修改任何官方代码**：只是把官方构建产物打包进 fpk，用 `HERMES_WEBAPP_DIST` 指过去
- 桌面入口：`app/ui/config` 用 `type: iframe`，飞牛桌面内嵌窗口打开（与原生应用的观感一致）

---

## 三、已验证的功能（实机）

在 NAS 上用 Playwright 实测，界面元素完整呈现：

**侧边栏**：Sessions / Bots / New session / Terminal
**管理面板**：Capabilities / Messaging / Artifacts / Scheduled jobs / New project / Profiles
**主区**：`NEW SESSION` + "Type a task, question, or snippet..."
**状态栏**：Backend 状态、Smart client 版本
**首次进入**：官方 onboarding 引导页（"Let's get you setup with Hermes Agent" → Nous Portal / Other providers / I have an API key）

**控制台错误：0**

### 认证链路（实测）

| 场景 | 结果 |
|---|---|
| `POST /auth/password-login` 正确密码 | ✅ 200 `{"ok":true,"next":"/"}` |
| 认证后 `GET /` | ✅ 200 `<title>Hermes</title>`，静态资源 200 |
| 错误密码 | ✅ 401 `Invalid credentials` |
| 未认证访问 | ✅ 302 跳转登录页 |

### 生命周期（实测）

| 步骤 | 退出码 |
|---|---|
| `install_callback` | 0 |
| `cmd/main start` | 0 |
| `cmd/main stop` | 0 |
| `cmd/main status`（运行时） | 0 |
| `cmd/main status`（停止后） | 3 |

---

## 四、实测踩过的坑（全部已固化进脚本）

这一节是本次最有价值的产出——**每个坑都是实机跑出来的**。

### 坑 1：PyPI 上的版本没有 `webapp` 子命令

- PyPI `hermes-agent` 最高仅 **0.19.0**，而 0.19.0 **没有 `hermes webapp`** 命令
- Webapp 是之后才加入的 → **必须装 GitHub 源码版**（editable）
- PyPI 最新版与 GitHub 最新版（v0.22.5）**严重脱节**

### 坑 2：源码要求 Python 3.14，不是 3.11

`pyproject.toml` 里依赖按 `python_version >= '3.14'` 声明：

```toml
"ruamel.yaml==0.18.16; python_version >= '3.14'",
```

但 `hermes_yaml.py` **无条件 import 它** → 3.11 环境直接 `ModuleNotFoundError: ruamel`。
**必须用 Python 3.14 建 venv**（uv 可自管下载）。

### 坑 3：uv 的 `exclude-newer` + 国内镜像 = 依赖全被过滤

现象：

```
error: No solution found when resolving dependencies
  cause: Because there are no versions of defusedxml ...
hint: `defusedxml` was filtered by `exclude-newer` to only include packages uploaded before 2026-10-29
```

根因（对照实验定位）：
- uv 0.13.6 **默认带 exclude-newer**（截止其构建日 2026-10-29）
- **国内 PyPI 镜像不携带 `upload-time` 元数据** → uv 无法判断上传时间 → 全部误判为"过新"而过滤
- 实测：`pypi.org` + 不设 exclude → 成功；清华源 + 任意 exclude → 失败

**解法**：`UV_INDEX_URL=https://pypi.org/simple` + `--exclude-newer 2027-01-01`

### 坑 4：NAS 连不上 github.com

```
Failed to connect to github.com port 443 after 135181 ms
```

**解法**：多路备援 —— git clone → codeload 直连 → gh-proxy.com → ghproxy.net → github 直连。
实测 `gh-proxy.com` 可用（200），且 `curl` 必须加 `--http1.1`（NAS 上 HTTP/2 有流异常）。

### 坑 5：uv 的下载 URL 不能拼

`https://pypi.org/simple/uv/uv-x86_64-unknown-linux-gnu.whl` → **404**。因为 wheel 文件名带版本号
（`uv-0.13.6-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`），各源命名还不一样。

**解法**：改用 **PyPI JSON API** 精确定位：
`GET https://pypi.org/pypi/uv/json` → 取 `info.version` → 匹配 `releases[version]` 里的 linux wheel → 拿 `url`。

### 坑 6：安装阶段拿不到 `TRIM_SERVICE_PORT`

fnOS 在安装阶段可能不注入该变量 → 我第一版写出的 config 里 `port:` 是**空的** → webapp 未真正启用。

**解法**：加兜底 + 数字校验：
```bash
SERVICE_PORT="${TRIM_SERVICE_PORT:-9120}"
case "${SERVICE_PORT}" in ''|*[!0-9]*) SERVICE_PORT="9120" ;; esac
```

### 坑 7：fnOS 的 `wizard/*` 必须是 JSON 数组

写成 shell 脚本 → `fnpack build` 直接报错 `File "install" is not valid due to JSON format`。

### 坑 8：`webapp --status` 说 "off" 是假象

它读的是 `~/.hermes/config.yaml`（默认路径），而我们的配置在 `HERMES_HOME`。**不影响实际运行**——
HTTP 200 与 UI 渲染才是真相。

---

## 五、交付物

### fpk 结构

```
hermes-desktop-fpk/
├── manifest                 # appname/version/platform/service_port=9120/checkport
├── app/
│   ├── ui/config            # iframe 入口 → 9120
│   └── webapp/              # 官方渲染层（1081 文件，47MB）
├── cmd/
│   ├── main                 # start/stop/status/restart/log
│   ├── install_init         # 清残留进程
│   ├── install_callback     # 装 uv → 拉源码 → Py3.14 venv → editable 安装 → 写配置
│   ├── upgrade_init/callback
│   ├── uninstall_init/callback
│   └── config_init/callback
├── config/{privilege,resource}
└── wizard/{install,upgrade,uninstall,config}   # JSON
```

### 安装向导（2 步）

**第 1 步 · 访问账号**
- 访问用户名（默认 admin）
- 访问密码（留空则**随机生成 20 位强密码**）
- 提示：登录后等同授予本机命令执行权限，勿公网暴露

**第 2 步 · 网络配置**
- GitHub 下载通道：自动探测（推荐）/ gh-proxy.com / ghproxy.net / codeload / 直连
- Python 依赖源：PyPI 官方（推荐，元数据完整）/ 清华 / 阿里云

### 关键设计

- **路径全部由 `TRIM_*` 推导**，不硬编码卷号（兼容 vol1/vol2/volX）
- **`HERMES_HOME` 指向 `TRIM_PKGVAR`** → 会话/记忆/技能/配置全在应用数据目录，卸载保留
- **`HERMES_WEBAPP_DIST` 指向打包的渲染层** → 运行时不需 Node、不需源码构建
- **端口占用自动回退** 9120 → 29220
- **错误写入 `TRIM_TEMP_LOGFILE`** → fnOS 以对话框展示给用户
- **访问信息写入 `access-info.txt`**（权限 600），用户可查随机密码

---

## 六、限制与说明

### 浏览器形态相比原生 Electron 少了什么

官方文档明确列出（属于 Electron 独有能力）：

- 全局快捷键、置顶浮窗、OS 窗口检查、外部终端唤起
- `safeStorage` 凭据加密存储
- 麦克风/语音、系统通知、剪贴板读取（**需 HTTPS 安全上下文**，HTTP 下浏览器禁用）
- 应用内更新器

### 保留下来的（Agent 核心价值全在）

- 完整对话：流式输出、工具调用可视化、结构化工具摘要
- 会话历史、Profiles 切换、Skills 管理、Cron 定时任务
- 文件浏览、并排预览、Artifacts、Git 审阅、终端栏
- 消息网关（微信/QQ/飞书/Telegram 等）、MCP 集成
- 与 CLI / Desktop / 网关共享同一套会话与队列

### 安全提醒

登录后的用户**等同于授予本机命令执行权限**（能跑 terminal 工具、读写文件、访问凭据）。
本应用默认绑定 `0.0.0.0:9120` 且强制密码认证。**切勿直接暴露到公网**；需要外网访问请用 VPN / 反代 + HTTPS。

---

## 七、构建产物说明

**渲染层 bundle** 的构建过程（在 e5 上完成）：

```bash
# 需要 Node >= 26（官方 build 脚本要求）
node apps/desktop/scripts/build-webapp.mjs --deps-only   # npm ci，跳过 Electron 二进制
node apps/desktop/scripts/build-webapp.mjs               # Vite 构建 → apps/desktop/dist-webapp
```

产出：`dist-webapp/` 1081 文件 / 47MB → tar.gz 16.8MB
SHA256：`1308778091ff2de00fa1c0569250bafabecc92db9e992732333736d0e78c8216`

> 注意：e5 宿主机缺 `libatomic.so.1`（Node 26 需要），从 Docker overlay 里取后用
> `LD_LIBRARY_PATH` 注入即可，无需 sudo。

---

## 八、下一步

1. **装到 NAS**：把 `hermes-desktop.fpk` 上传到飞牛 → 应用中心 → 手动安装
2. **首次配置**：向导设用户名密码 → 桌面图标打开 → 按引导填模型 API Key
3. **装官方 fnOS Skill**：让 Agent 获得 NAS 操控能力
4. **与本机 Windows Desktop 并存**：两边连同一套 gateway 会话，任务队列互通

---

## 附：核实来源

- Hermes 源码：`NousResearch/hermes-agent`（`hermes_cli/webapp.py`、`hermes_cli/dashboard_auth/routes.py`、`plugins/dashboard_auth/basic/`、`gateway/webapp_listener.py`）
- 官方文档：`website/docs/user-guide/desktop.md`（"Use the Desktop workspace from a browser"）、`user-guide/configuration.md`（webapp 段）、`reference/cli-commands.md`
- fnOS 文档：`developer.fnnas.com/docs/core-concepts/framework/`、`/docs/cli/fnpack`、`/docs/core-concepts/environment-variables/`
- fnpack：`static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64`
- 实机环境：飞牛 NAS <nas-ip>（Debian 12 bookworm / x86_64 / 23GB RAM / 30TB 可用）

---

## 附录：自动化仓库（fnos-hermes-desktop）

### 仓库结构

```
fnos-hermes-desktop/
├── .github/workflows/
│   ├── build.yml              # 定时/手动构建 + 发 Release
│   └── sync.yml               # 每 6 小时检测上游变更，有变化才触发构建
├── scripts/
│   ├── build.sh               # 完整构建：克隆上游 → 构建渲染层 → 打包 fpk
│   ├── smoke.sh               # 快速冒烟（约 1 分钟，不编译渲染层）
│   └── push.sh                # 一键创建远程仓库 + 推送 + 触发 CI
├── packaging/fpk/             # fpk 模板（渲染层构建时注入，不入库）
├── docs/IMPLEMENTATION.md
└── README.md / README.en.md
```

### 工作流设计

| 工作流 | 触发 | 职责 |
|---|---|---|
| `sync.yml` | 每 6 小时 | 对比上游 commit，写入 `.upstream-sha`，有变化才 dispatch |
| `build.yml` | 每日 03:17 / 手动 / 脚本变更 | 完整构建 → 上传 artifact → 发 Release（tag 已存在则只补传附件） |

两者配合：`sync.yml` 反应快（6 小时），`build.yml` 兜底（每天必构建一次）。

### 构建验证结果（e5 实测）

从 `git archive` 导出的纯净快照开始，等价于 CI 的干净克隆：

```
[build] 上游最新版本 0.22.5
[build] 渲染层就绪：1081 个文件，47M
[build] 完成：dist/hermes-desktop.fpk
[build]   版本   0.22.5（上游 a4994d4e）
[build]   大小   16728259 字节
build.sh exit=0
```

产物校验：22 个包内条目 / 1087 个渲染层文件 / manifest 字段正确。

### 产物在 NAS 上的实机验证

CI 构建出的 fpk 传到 NAS 后解包安装：

```
[1/5] 解包 → 1081 个渲染层文件
[2/5] 脚本换行符 → ✓ 全部 LF
[3/5] install_callback → ✓ hermes vgit.a4994d4 就绪，webapp 子命令可用
[4/5] 启动 → gateway running，登录 200，GET / 200 <title>Hermes</title>
```

**完整闭环：源码 → 自动构建 → fpk → NAS 安装 → 登录可用。**

### 版本号获取

浅克隆拿不到 tag，改用 GitHub API：

```bash
curl -fsSL https://api.github.com/repos/NousResearch/hermes-agent/releases/latest \
  | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p'
```

### 换行符问题（必须处理）

Windows 上开发、Linux 上执行，`cmd/` 下的脚本若被转成 CRLF 会直接
`bad interpreter: /bin/bash^M`。已用 `.gitattributes` 强制：

```gitattributes
*.sh text eol=lf
packaging/fpk/cmd/* text eol=lf
```

### 踩坑补充

- `fnpack` 没有 `--version` 参数（Windows 版有，Linux 版无）→ 改为打印已知版本号
- GitHub Actions 的 YAML 多行字符串里，notes 用 `--notes-file` 传文件更稳，
  直接写多行标量会被 `:` 等字符干扰解析
- 上游 `pyproject.toml` 的 `version = "0.0.0"`（构建期占位），
  真实版本只在 git tag 上 → 必须走 API 或指定 tag ref

---

## 附录二：内置飞牛官方 Skill

### 决策结论

**内置，但只内置能自动化的部分。** 三部分分别处理：

| 组成 | 大小 | 处理 |
|---|---|---|
| 知识文档（SKILL.md + entries + reference） | ~500 KB | 内置 |
| trim-cli 二进制（linux-x64 / arm64） | 7.3 / 6.2 MB | 内置（安装时按架构裁剪） |
| OAuth 授权 | — | **不可内置**，必须用户交互 |

### 实测依据

在 NAS 上实测 trim-cli（官方 V2 版，含 2FA/RSA 支持）：

```
$ trim-cli --help
Commands: app baidu-netdisk docker download file media photos
          diagnostic-log logger login logout monitor network power
          raw storage system user +ls +search +status

$ trim-cli +status
Error: failed to get system info from localhost:5666:
       saved proxy session is required; run login first

$ trim-cli login --no-open
Open this URL to authorize trim-cli:
http://localhost:5666/signin?client_id=YJNMPJUGA9&scope=trim.user.all ...
         &code_challenge_method=S256&device_id=trim-cli-...
```

结论：
- trim-cli **自动发现本机 fnOS**（localhost:5666），无需配置地址
- 模块齐全，覆盖 NAS 全部管理能力
- 论坛曾报告的「不兼容 2FA」问题在 V2 版已修复（本机 skill 的 oauth.md 明确写了 2FA/TOTP 支持）

### 落点

上游源码依据（`hermes_constants.py`）：

```python
def get_skills_dir() -> Path:
    """Return the path to the skills directory under HERMES_HOME."""
    return get_hermes_home() / "skills"
```

本应用 `HERMES_HOME = TRIM_PKGVAR`，故 Skill 落在应用数据目录，
卸载保留、重装免重新授权。

### 踩坑：postinstall 覆盖竞态

**现象**：首次安装后 `hermes skills list` 只有 builtin，看不到 fnos。

**根因**：editable 安装的 `postinstall` 钩子会在安装结束后铺开 Hermes 内置技能
（`skills/apple`、`skills/creative` 等 39 个），**覆盖**刚部署到
`HERMES_HOME/skills` 的 `fnos-trim`。

**修复（三重保险）**：

1. `install_callback` 增加 `verify_fnos_skill`：部署后校验 `SKILL.md` + `bin/` +
   `scripts/trim-cli` 是否在位，不在则重新部署（最多 3 次，每次间隔 2s）
2. `cmd/main` 增加 `heal_fnos_skill`：启动时若发现 Skill 缺失，从只读的安装目录
   （`TRIM_APPDEST/skills/fnos-trim`）补回
3. `start_process` 在启动前调用 `heal_fnos_skill`

**实测**（模拟覆盖后）：

```
[3] 校验并自愈 → 校验未通过 → 重新部署 → 校验通过 → ✓ 已恢复
[4] 启动自愈   → 飞牛 Skill 缺失，从安装目录补回 → ✓ 启动自愈生效
[5] Hermes 技能发现 → fnos │ local │ local │ enabled
                    0 hub-installed, 39 builtin, 1 local — 40 enabled
```

### 内置后体积

| 项 | 大小 |
|---|---|
| 渲染层（webapp） | 47 MB / 1081 文件 |
| 飞牛 Skill | 14 MB / 40 文件（含两个架构二进制） |
| 打包后 fpk | 22.9 MB |

安装时按架构裁剪二进制，实际占用约 7.3 MB（x86_64）。
