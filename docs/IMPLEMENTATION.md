# Hermes Desktop 飞牛原生版 — 实施报告

> 环境：fnOS / x86_64 / N5095 ｜ 2026-11-02
> 目标：NAS 上像本地一样用 Hermes Desktop，且**不改一行官方代码**
> 状态：**网页入口可用**；**桌面内嵌为实验性**（依赖飞牛网关透传 WebSocket）

---

## 一、结论与架构

把 Hermes 官方桌面端的 **Webapp bundle**（`dist-webapp`）打包成 fnOS 应用，在 NAS 上运行
上游的 `hermes gateway`，浏览器访问即得到**原版 Desktop 界面** —— 官方 Desktop UI 与
浏览器 Dashboard 本就共用同一套传输层、会话与工具，所以承载的是真品而非复刻。

```
   局域网用户 ─────▶ http://<nas>:9120          浏览器直连，Hermes basic_auth 把关 ──┐
                                                                                    │
   飞牛桌面用户 ───▶ http://<nas>:5666/app/hermes-desktop                           │
                     fnOS 统一网关，校验飞牛登录态                                    │
                            │ app.sock                                              │
                            ▼                                                       │
                     gateway_proxy.py（网关适配层）                                   │
                     · 剥离 /app/hermes-desktop 前缀                                 │
                     · HTML 改写（子路径基准、桥接注入）                              │
                     · WebSocket 升级转发                                            │
                            │ 127.0.0.1:9120                                        │
                            ▼                                                       │
                     hermes gateway（webapp 渲染层）◀───────────────────────────────┘
                     Agent / 工具 / 会话 / 技能 / 定时任务
```

两个入口访问**同一个后端、同一份会话**。

| 端口 | 监听 | 用途 |
|---|---|---|
| `9120` | `0.0.0.0` | Hermes webapp，网页入口直连 |
| `9120` | `127.0.0.1` | 适配层的上游目标 |
| `9121` | `127.0.0.1` | 适配层 TCP 通道（仅本机，排障用） |
| `5666` | fnOS | 统一网关，内嵌入口经此进入 |

---

## 二、关键设计决策

| 决策 | 理由 |
|---|---|
| 用 Webapp bundle，不用 Electron | 官方 Linux 发行腿已禁用，NAS 无显示环境；`webapp.py` 明确这是"浏览器里的 Desktop 工作区" |
| 绑 `0.0.0.0` + **必须**配 `basic_auth` | 见下，最容易踩且最致命 |
| 内嵌入口走统一网关 | iframe 直连 `:9120` 属跨源，`SameSite=lax` 会话 Cookie 不发送，登录态维持不住 |
| 桌面入口用 `type: url` + **应用端口** | 官方惯例；`port` 写成宿主端口会让整份 `ui/config` 失效 |
| 端口被占只清理、不换端口 | 早期顺延端口并写回 `config.yaml`，一次临时占用导致永久漂移 |

### 绑定地址与认证门（坑王）

上游 `src/hermes_cli/web_server_boundary.py`：

```python
def should_require_auth(host: str, allow_public: bool = False) -> bool:
    """True iff the auth gate must be active: any non-loopback bind."""
    return host not in _LOOPBACK_HOST_VALUES
```

**只看配置里的绑定地址，不看请求的 Host 头。** 后果：

- 绑 `0.0.0.0` → 强制认证门；没有 auth provider 时**拒绝启动 webapp**
- 表现极具误导：gateway 主循环照跑、进程活着、**端口不监听**，
  外部看到「打不开 / 502」，日志只有一句 ERROR
- **白名单绕不过**：`trusted_public_hosts` 只参与 Host 头校验（`_is_accepted_host`），与认证判定无关

认证要求 scrypt 哈希，不能写明文：

```bash
cd /vol1/@appdata/hermes-desktop
HERMES_PW='你的密码' PYTHONPATH=src venv/bin/python3 -c \
  'import os; from plugins.dashboard_auth.basic import hash_password; \
   print(hash_password(os.environ["HERMES_PW"]))'
```

> 试过「绑回环免登录 + 适配层转发」，技术上可行但适配层一旦对局域网开端口，
> 就是**局域网任何人无需认证即可操作 Agent**。已放弃。

### `ui/config` 的 port 反例

曾为让网页入口走网关，把 `ui/config` 里的 `port` 写成 `"5666"`（宿主端口），
结果 **fnOS 整份 `ui/config` 失效、所有图标都打不开**。`port` 必须是应用自己的端口。

---

## 三、运行期踩坑

### A. 数据目录属主污染 → 静默退出

Hermes 以 `hermes-desktop` 身份运行。用别的账号（如 SSH 管理员）手工跑过 `hermes`，
会留下外属主文件：`webapp/session-token`、`gateway.lock`、`tools/`、`cron/ticker_heartbeat`。
应用身份读写不了，**在日志初始化之前就退出** —— 日志一行错都没有，只有启动横幅一闪而过。

```bash
find /vol1/@appdata/hermes-desktop ! -user hermes-desktop
```

纪律：不要用别的账号手动跑 Hermes，排障用 Web UI 的终端栏（那里是应用身份）。

### B. 日志要看对地方

`cmd/main` 写的 `info.log` 往往只有启动横幅。真实错误在：

```
$DATA_DIR/logs/gateway.log
$DATA_DIR/logs/gateway-exit-diag.log        # JSON 行，含 start / asyncio.run.returned / exit_nonzero
$DATA_DIR/logs/gateway-shutdown-diag.log
```

### C. WebSocket 不能用 `http.client` 转发

前端报 `Desktop IPC bridge is unavailable` —— 那条"IPC 桥"就是 WebSocket。适配层最初用
`http.client`，它读完一个完整响应就结束，**扛不住握手后的帧流**。

修法：检测到 `Upgrade: websocket` 后切裸 socket 双向对拷，握手阶段改写
`Host`/`Origin`/`Referer`，收到 `101` 后 `select` 轮询两端。

### D. 子路径下前端跳回站点根

前端用 `window.__HERMES_BASE_PATH__` 拼所有路由，上游默认注入空串。在网关子路径下
`location.href = basePath + "/"` 变成站点根 `/`，浏览器解析成 `http://<nas>:5666/`，
**落到飞牛桌面**。

修法：在 `rewrite_html` 里改成网关前缀。比钩 `location.href` 可靠 —— 那个 setter
多数浏览器无法覆盖，直接赋值会绕过桥接脚本。

### E. `webapp-launch` 返回 `sign_in` 时不能拿来重定向

```json
{"ok": true, "result": {"url": "http://127.0.0.1:9120/", "sign_in": true}}
```

这是"请先去登录"的根地址，不是一次性启动链接。当前缀拼接得到 `/app/hermes-desktop/`，
302 到自己 —— 浏览器无限重定向，iframe 里表现为空白。识别 `sign_in` 就直接放行，
让上游自己 302 到 `/login`。

### F. 卸载残留导致无法安装

`创建虚拟环境失败：A directory already exists at .../venv`。卸载选了「保留数据」或中断，
`venv` 留在原地，`uv` 拒绝在已存在目录上创建。改为 `uv venv --clear`，并加兜底：
残留目录不完整（缺 `bin/python3`）就先删再建。

### G. 插件装了三处却都不生效（浏览器只认 bundled）

这个生态有**三套互不相通的插件机制**：

| 机制 | 位置 | 作用 | 浏览器可用 |
|---|---|---|---|
| Agent 插件 | `<home>/src/plugins/<id>/{plugin.yaml,__init__.py}` | 注册 agent 工具 | ✓（后端） |
| Dashboard 插件 | `<home>/plugins/<id>/dashboard/{manifest.json,plugin_api.py}` | `/api/dashboard/plugins` 列表、后端 API | 仅后端 |
| Desktop 插件 | `<home>/desktop-plugins/<id>/plugin.js` | 前端 UI（tab/pane/chip） | **✗ Electron 专属** |

前端加载磁盘插件的代码：

```ts
async function diskRoots() {
  const root = await window.hermesDesktop?.desktopPluginsRoot?.()
  return root ? [{ dir: root, entrySegments: ['plugin.js'] }] : []
}
```

`window.hermesDesktop` 是 **Electron 的 IPC 桥**，纯浏览器里 `undefined` → `diskRoots()`
返回空数组 → **磁盘插件一个都不加载**。而 `/api/desktop-plugins/list|read` 全程 200，
那只是"能力页的文件浏览"在枚举目录，**不是加载器** —— 这解释了为什么 plugin.js
被读取却不执行、也不报错。

浏览器版唯一的路是 **bundled**：`src/apps/desktop/src/plugins/<id>/plugin.{js,tsx}`，
由 `import.meta.glob` 在**构建时**打进 main bundle。`scripts/build.sh` 因此增加了
`inject_bundled_plugins()`，构建前把随包插件复制到源码树对应位置。

### H. 插件 UI 的 API 换代了

旧写法 `window.__HERMES_PLUGINS__.register(id, Page)` 里的全局**在源码前端中不存在**，
脚本第一行 `if (!SDK) return` 就静默返回。当前用 contributions：

```js
export default {
  id: 'fnos-auth',
  register(ctx) {
    ctx.register({ area: 'routes', id: '...:page', title: '...',
                   data: { path: '/fnos-auth' }, render: () => jsx(Page, {}) })
  }
}
```

`ctx` 提供 `register / registerMany / registerSettingsPage / rest / socket / onEvent /
onDispose / setTimeout / i18n`；后端调用走 `ctx.rest('/devices')`，自动加 `/api/plugins/<id>/` 前缀。

可用 `area` 只有 `layouts`、`panes`、`routes`、`statusBar.left|right`、`titleBar.*` ——
**没有 nav / sidebar**，插件无法插入左侧导航。

组件从 `@hermes/plugin-sdk` 取（`Badge / Button / Input / Tip / Dialog / Switch / Tabs /
Toast / SearchField / cn / icons / useValue / useQuery …`）。两个坑：`Button` 的 variant
**没有 `muted`**（那是 Badge 的）；UI 必须用 `jsx()`/`jsxs()`，不能用 JSX 语法（加载时不经编译）。

### I. 图标"太小"

不是尺寸问题（官方只用 `64.png`），是两点：原图 1254×1254 但内容边界只有 817×776，
四周留白约 40%，缩到 64px 后图形只剩约 42px；且标志是中性灰线稿 + 透明底，深色桌面上对比度极低。

处理：`getbbox()` 裁到内容边界 → 放浅色圆角底（内边距 2%），内容占比 96%。

---

## 四、构建期踩坑

都已固化进 `scripts/build.sh` 与 `packaging/fpk/cmd/`。

| # | 问题 | 处理 |
|---|---|---|
| 1 | PyPI 版本没有 `webapp` 子命令 | 必须从源码安装 |
| 2 | 源码要求 **Python 3.14** | uv 建 3.14 venv，3.12 仅回退 |
| 3 | uv 的 `exclude-newer` + 国内镜像 = 依赖全被过滤 | 走官方 pm 安装（自带哈希锁定） |
| 4 | NAS 直连 github.com 不稳 | 构建在 e5 完成，只传产物 |
| 5 | uv 下载 URL 不能手工拼 | 用 uv 自身命令 |
| 6 | 安装阶段拿不到 `TRIM_SERVICE_PORT` | 以 manifest 的 `service_port` 兜底 |
| 7 | `wizard/*` 必须是 JSON 数组 | 写成 shell 会让 `fnpack build` 直接报错 |
| 8 | `webapp --status` 报 "off" 是假象 | 以端口是否监听为准 |
| 9 | `postinstall` 阶段写 Skill 存在覆盖竞态 | 改为安装后置步骤 |
| 10 | 构建全绿但 Releases 没有 fpk | 见下 |

构建需要 **Node ≥ 26**（官方 build 脚本要求）。e5 上备有 `/tmp/node26` 与
`/tmp/extra-libs`（glibc 补丁）。

### 坑 J：构建全绿 ≠ 有东西可下载

现象：Actions 绿色、artifact 存在，但 Releases 只有 `Source code`，`git ls-remote --tags`
也查不到 CI 打的 tag。两个原因叠加：

1. **附件上传端点不同**：创建 Release 走 `api.github.com`，但上传附件必须走
   `uploads.github.com` —— 传错 host 不会报错退出，结果是「Release 建了，Assets 是空的」
2. **`continue-on-error: true` 让失败伪装成绿色**：API 里 `conclusion=success`、
   `outcome=failure`，UI 上看不出来

另外仓库级 `Settings → Actions → General → Workflow permissions` 默认 Read，会**覆盖**
workflow 里的 `permissions: contents: write`，推 tag 和建 Release 都 403。

处理：附件改传 `uploads.github.com` + raw body；失败时输出 `::error::` 并 `exit 1`；
去掉独立的 `git push tag`（`POST /releases` 的 `tag_name` 不存在时会自动建 tag）；
`publish_release.py` 把附件上传失败改为非零退出。

---

## 五、交付物

```
hermes-desktop.fpk
├── app.tgz              # 渲染层 + 内置飞牛 Skill（约 23 MB）
├── cmd/
│   ├── main             # 生命周期：start / stop / status
│   ├── install_callback # 装 uv → 拉源码 → venv → editable 安装 → 写配置
│   ├── install_init / upgrade_callback / uninstall_callback
│   ├── gateway_proxy.py # 网关适配层（前缀剥离、HTML 改写、WS 转发）
│   └── fnos_bridge.py   # 注入页面的前缀桥接脚本
├── config/              # privilege / resource
├── wizard/              # install / config / upgrade / uninstall（JSON 数组）
├── app/plugins/         # 随包插件（fnOS 授权面板）
├── ICON.PNG / ICON_256.PNG
└── manifest
```

安装时生成 `config.yaml`：

```yaml
webapp:
  enabled: true
  host: 0.0.0.0        # 局域网可访问；因此必须配认证
  port: 9120
  public_url: ""

dashboard:
  basic_auth:          # 缺这一段，webapp 不会启动
    username: admin
    password_hash: "scrypt$..."   # 哈希，非明文
```

`install_callback` 用 venv 里的 `hash_password` 现场生成；生成失败时自动回退
`host: 127.0.0.1`，保证应用至少能起来。

---

## 六、已知限制

**桌面内嵌**要真正可用，需同时满足：网关透传 `Upgrade: websocket`、代理层把路由挂到
子路径（已解决）、WS 端点凭证在子路径下仍有效。第 1、3 项取决于飞牛网关实现，本项目
无法控制。提示 `Desktop IPC bridge is unavailable` 时**改用网页入口**。

**相比 Electron 少了**：系统托盘 / 原生通知、原生文件选择器路径、自动更新、桌面级硬件加速。
Agent 核心能力（工具调用、代码执行、文件读写、定时任务、多 Profile、技能、MCP、持久记忆）全部保留。

**安全**：登录后等同于授予本机命令执行权限。切勿直接暴露公网；外网请用 VPN / Tailscale 或反代 + HTTPS。

---

## 七、排障速查

| 现象 | 先看 | 常见原因 |
|---|---|---|
| 点启动失败 | `logs/gateway.log` | 缺 `dashboard.basic_auth` |
| 启动横幅一闪而过 | `find $DATA_DIR ! -user hermes-desktop` | 数据目录属主污染 |
| 9120 不监听 | `grep -A3 '^webapp:' config.yaml` | 端口漂移 / 绑定地址与认证不匹配 |
| 网页入口 502 | `ss -ltn \| grep 9120` | 上游未启动 |
| 内嵌空白/转圈 | 适配层 `gateway-proxy.log` | 302 死循环（`sign_in` 未识别） |
| 内嵌提示 IPC bridge | 浏览器 Network 看 WS | 网关未透传 Upgrade |
| 登录后跳飞牛桌面 | 页面里搜 `__HERMES_BASE_PATH__` | 未被改写 |
| 安装报 venv 已存在 | — | 卸载残留，`rm -rf venv` 后重装 |
| Releases 无 fpk | 那次运行的日志 | 附件 host 传错 / token 只读 |

---

## 八、与官方安装脚本的关系

官方 `install.sh` 假设系统级 Python 与可写 `/usr/local`，与 fpk 沙箱模型不兼容。
本项目按官方脚本的**阶段划分**重新实现，但不复用其脚本：uv 建 3.14 venv → 官方 `pm`
安装依赖（哈希锁定 + 自动剥离 `exclude-newer`）→ `pm` 不可用时回退 `pip install`。

不使用官方 `hermes setup`（交互式，且在 fpk 里会写入错误的 HOME）与
`hermes gateway install`（面向 systemd，而 fnOS 用自己的生命周期管理）。

---

## 附：核实来源

| 结论 | 依据 |
|---|---|
| Webapp 与 Dashboard 共用传输层 | `src/hermes_cli/webapp.py` 模块文档 |
| 非回环绑定强制认证 | `src/hermes_cli/web_server_boundary.py: should_require_auth()` |
| 白名单只管 Host 校验 | 同文件 `_is_accepted_host()` |
| 认证要求 scrypt 哈希 | 运行时日志 + `plugins/dashboard_auth/basic.py` |
| 子路径基准变量 | 上游注入的 `window.__HERMES_BASE_PATH__` |
| 插件三套机制 / 浏览器只认 bundled | `contrib/runtime-loader.ts: diskRoots()` |
| 官方应用入口惯例 | `conversun/fnos-apps` 全量 `ui/config` 抽样 |
