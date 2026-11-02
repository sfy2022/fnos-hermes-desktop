# Hermes Desktop 飞牛原生版 — 实施报告

> 日期：2026-11-02 ｜ 目标：让 NAS 上像本地一样使用 Hermes Desktop，且**不改一行官方代码**
> 状态：**网页入口可用**；**桌面内嵌为实验性**（依赖飞牛网关透传 WebSocket）
> 实机环境：fnOS / x86_64 / N5095

本文记录**怎么做的**与**为什么这么做**。凡是踩过的坑都写了判定方法，
便于后来者（包括未来的自己）少走弯路。

---

## 一、结论

把 Hermes 官方桌面端的 **Webapp bundle**（`dist-webapp`）打包成 fnOS 应用，
在 NAS 上运行上游的 `hermes gateway`，浏览器访问即得到**原版 Desktop 界面**。

不改动任何上游代码，不跑 Electron，不需要虚拟显示串流。官方 Desktop 的 UI
与浏览器 Dashboard 本就共用同一套传输层、同一套会话与工具，所以这里承载的是
**真品**而非复刻。

代价是浏览器形态的固有限制（见「已知限制」），核心 Agent 能力一项不少。

---

## 二、架构

```
                    ┌──────────────────────────────────────┐
   局域网用户 ─────▶│  http://<nas>:9120    网页入口        │──┐
                    │  浏览器直连，Hermes basic_auth 把关    │  │
                    └──────────────────────────────────────┘  │
                                                              │
                    ┌──────────────────────────────────────┐  │
   飞牛桌面用户 ───▶│  http://<nas>:5666/app/hermes-desktop │  │
                    │  fnOS 统一网关，校验飞牛登录态         │  │
                    └───────────────┬──────────────────────┘  │
                                    │ app.sock                │
                    ┌───────────────▼──────────────────────┐  │
                    │  gateway_proxy.py（网关适配层）        │  │
                    │  · 剥离 /app/hermes-desktop 前缀       │  │
                    │  · HTML 改写（子路径基准、桥接注入）    │  │
                    │  · WebSocket 升级转发                 │  │
                    └───────────────┬──────────────────────┘  │
                                    │ 127.0.0.1:9120          │
                    ┌───────────────▼──────────────────────┐◀─┘
                    │  hermes gateway（webapp 渲染层）      │
                    │  Agent / 工具 / 会话 / 技能 / 定时任务 │
                    └──────────────────────────────────────┘
```

两个入口访问**同一个后端、同一份会话**。

### 端口分工

| 端口 | 监听 | 用途 |
|---|---|---|
| `9120` | `0.0.0.0` | Hermes webapp，网页入口直连 |
| `9120` | `127.0.0.1` | 适配层的上游目标 |
| `9121` | `127.0.0.1` | 适配层 TCP 通道（仅本机，排障用） |
| `5666` | fnOS | 统一网关，内嵌入口经此进入 |

---

## 三、关键设计决策

### 1. 用 Webapp bundle，不用 Electron

官方 Linux 发行腿已禁用，NAS 也没有显示环境。而 `hermes_cli/webapp.py` 明确写着：

> *Build and launch plumbing for the browser-hosted **Desktop workspace**. The browser
> surface **shares Hermes' hardened dashboard server**... it never creates a second
> HTTP/WebSocket stack.*

即官方 Desktop UI 本身就是浏览器应用，打包渲染层即可，无需改造。

### 2. 绑定 `0.0.0.0`，并且**必须**配 `dashboard.basic_auth`

这是本项目最容易踩、也最致命的一条。上游
`src/hermes_cli/web_server_boundary.py`：

```python
def should_require_auth(host: str, allow_public: bool = False) -> bool:
    """True iff the auth gate must be active: any non-loopback bind."""
    return host not in _LOOPBACK_HOST_VALUES
```

**它只看配置里的绑定地址，不看请求的 Host 头。** 因此：

- 绑 `0.0.0.0` → 强制认证门；没有 auth provider 时**拒绝启动 webapp**
- 拒绝的表现极具误导性：gateway 主循环照跑、进程活着、**端口不监听**，
  外部看到的是「打不开 / 502」，日志里只有一句 ERROR
- **白名单绕不过**：`trusted_public_hosts` 只参与 Host 头校验（`_is_accepted_host`），
  与认证判定无关

认证要求 `password_hash`（**scrypt，不是明文**）：

```bash
cd /vol1/@appdata/hermes-desktop
HERMES_PW='你的密码' PYTHONPATH=src venv/bin/python3 -c \
  'import os; from plugins.dashboard_auth.basic import hash_password; \
   print(hash_password(os.environ["HERMES_PW"]))'
```

> 曾试图「绑回环免登录 + 适配层转发」换取免登录 —— 技术上可行，但适配层一旦
> 对局域网开端口，就是**局域网任何人无需认证即可操作 Agent**（执行命令、读写文件）。
> Hermes 的一次性启动链接本意是限制「同 OS 用户」，代理代取会绕过它。已放弃该方案。

### 3. 内嵌入口走统一网关（实验性）

原因：iframe 直接指向 `:9120` 属跨源，`SameSite=lax` 的会话 Cookie 不会发送，
登录状态维持不住。走 `:5666` 网关则与飞牛桌面同源，且网关会校验飞牛登录态。

代价是引入三层复杂性（前缀改写、WS 转发、凭证域），详见「已知限制」。

### 4. 桌面入口用 `type: url` + 应用端口

参考官方示例集合 `conversun/fnos-apps`：**绝大多数应用**都是

```json
{ "type": "url", "protocol": "http", "port": "<应用端口>", "url": "/" }
```

即点图标在浏览器打开应用端口。官方开发指南里也没有统一网关的专门章节，
其 iframe 示例同样是 `protocol` + `port` 直连。

**一个反例必须记住**：`ui/config` 里的 `port` 必须是**应用自己的端口**。
曾为让网页入口走网关而写成 `port: "5666"`（宿主端口），结果 **fnOS 整个
`ui/config` 都失效，所有图标都打不开**。

### 5. 不换端口，只清理占用者

早期实现里，`pick_port` 在 9120 被占用时会顺延到 `29220-29259`，并把结果
**永久写进 `config.yaml`**。一次临时占用就让端口永久漂移，之后与 `ui/config`
里的 9120 对不上 —— 表现为「启动成功但端口不监听」。

现在改为：端口被占就先清理同 home 的残留实例，清不掉就**明确失败**并提示，
绝不悄悄换端口。

---

## 四、运行期踩坑（这轮的主要发现）

### 坑 A：数据目录混入其他属主的文件 → 应用身份静默退出

**这是最隐蔽的一个。** Hermes 以 `hermes-desktop` 身份运行。若用其他账号
（例如 SSH 登录的管理员）手工跑过 `hermes`，会留下该账号属主的文件：
`webapp/session-token`、`gateway.lock`、`tools/`、`cron/ticker_heartbeat` 等。

应用身份读写不了这些文件，**会在日志初始化之前就退出** —— 日志里一行错都没有，
只有启动横幅一闪而过。

```bash
# 排查
find /vol1/@appdata/hermes-desktop ! -user hermes-desktop

# 修复：删掉让应用身份重建（先确认无重要数据）
```

**纪律：永远不要用别的账号手动跑 Hermes。** 排障请用 Web UI 的终端栏
（那里是应用身份）。

### 坑 B：日志要看对地方

`cmd/main` 写的 `info.log` 往往只有启动横幅。真实错误在：

```
$DATA_DIR/logs/gateway.log
$DATA_DIR/logs/gateway-exit-diag.log
$DATA_DIR/logs/gateway-shutdown-diag.log
```

`gateway-exit-diag.log` 是 JSON 行，含 `gateway.start` / `asyncio.run.returned` /
`gateway.exit_nonzero` 三类事件，配合 PID 能判断进程是正常退出还是被杀。

### 坑 C：WebSocket 不能用 `http.client` 转发

前端报：

```
Hermes couldn't start — Desktop IPC bridge is unavailable.
```

「IPC 桥」就是那条 WebSocket。适配层最初的 socket 通道用 `http.client`，
它只读一个完整 HTTP 响应就结束，**扛不住握手后的帧流**。

修法：检测 `Upgrade: websocket` 后切裸 socket 双向对拷 —— 握手阶段改写
`Host`/`Origin`/`Referer`，收到 `101` 后 `select` 轮询两端转发。
TCP 通道本来就是裸转发，天然支持。

### 坑 D：子路径下前端跳回站点根

Hermes 前端用 `window.__HERMES_BASE_PATH__` 拼所有路由与跳转，上游默认注入空串。
在网关子路径下，`location.href = basePath + "/"` 就变成站点根 `/` —— 浏览器解析成
`http://<nas>:5666/`，**落到飞牛桌面**。

修法：在 `rewrite_html` 里把它改成网关前缀。这比钩 `location.href` 可靠得多 ——
那个属性的 setter 在多数浏览器里无法覆盖（桥接脚本只能钩 `assign`/`replace`，
直接赋值绕过了它）。

### 坑 E：`webapp-launch` 返回 `sign_in` 时不能拿来重定向

配了 `basic_auth` 后，控制 socket 的 `webapp-launch` 回的是：

```json
{"ok": true, "result": {"url": "http://127.0.0.1:9120/", "sign_in": true}}
```

这是「请先去登录」的根地址，**不是一次性启动链接**。若把它当前缀拼接，
得到 `/app/hermes-desktop/` —— 302 到自己，浏览器无限重定向，iframe 里表现为空白。

修法：识别 `result.sign_in` 就直接放行，让上游自己 302 到 `/login`。

### 坑 F：卸载残留导致无法安装

```
无法安装 hermes-desktop
创建虚拟环境失败：A directory already exists at .../venv
```

卸载时选了「保留数据」或卸载中断，`venv` 留在原地，`uv` 拒绝在已存在目录上创建。
已改为 `uv venv --clear`，并加兜底：残留目录不完整（缺 `bin/python3`）就先删再建。

### 坑 H：插件装了三处的误区（浏览器只认 bundled）

给应用加 dashboard 面板时，把插件放哪都不出现，日志和浏览器控制台全程干净。
原因是这个生态里有**三套互不相通的插件机制**：

| 机制 | 位置 | 作用 | 浏览器可用 |
|---|---|---|---|
| Agent 插件 | `<home>/src/plugins/<id>/{plugin.yaml,__init__.py}` | 注册 agent 工具 | ✓（后端） |
| Dashboard 插件 | `<home>/plugins/<id>/dashboard/{manifest.json,plugin_api.py}` | `/api/dashboard/plugins` 列表、后端 API 挂载 | 仅后端 |
| Desktop 插件 | `<home>/desktop-plugins/<id>/plugin.js` | 前端 UI（tab/pane/chip） | **✗ Electron 专属** |

**关键**：前端加载磁盘插件的那段代码是

```ts
async function diskRoots() {
  const root = await window.hermesDesktop?.desktopPluginsRoot?.()
  return root ? [{ dir: root, entrySegments: ['plugin.js'] }] : []
}
```

`window.hermesDesktop` 是 **Electron 的 IPC 桥**，纯浏览器里是 `undefined`，
于是 `diskRoots()` 返回空数组 —— **磁盘上的插件一个都不会加载**。

而 `/api/desktop-plugins/list` 与 `/read` 全程 200，那只是「能力页的文件浏览」
在枚举目录，**不是加载器**。这就解释了为什么 plugin.js 明明被读取（200）
却从不执行、也不报错。

**浏览器版唯一的路是 bundled**：

```
src/apps/desktop/src/plugins/<id>/plugin.{js,tsx}
```

由 `import.meta.glob('../plugins/*/plugin.{js,ts,tsx}')` 在**构建时**打进 main bundle。
所以 `scripts/build.sh` 增加了 `inject_bundled_plugins()`，构建前把随包插件
复制到源码树对应位置。

### 坑 I：插件 UI 的 API 换代了

旧插件用 `window.__HERMES_PLUGINS__.register(id, Page)` —— 这个全局**在源码前端里
根本不存在**（`grep` 不到），脚本第一行 `if (!SDK) return` 就静默返回。

当前版本的写法是 contributions：

```js
export default {
  id: 'fnos-auth',
  register(ctx) {
    ctx.register({ area: 'routes', id: '...:page', title: '...',
                   data: { path: '/fnos-auth' }, render: () => jsx(Page, {}) })
  }
}
```

`ctx` 提供 `register / registerMany / registerSettingsPage / rest / socket /
onEvent / onDispose / setTimeout / i18n`。后端调用走 `ctx.rest('/devices')`，
自动加 `/api/plugins/<id>/` 命名空间。

可用的 `area` 只有：`layouts`、`panes`、`routes`、`statusBar.left|right`、
`titleBar.center|left|right` —— **没有 nav / sidebar**，插件无法往左侧导航插入口。

组件从 `@hermes/plugin-sdk` 取：`Badge / Button / Input / Tip / Tooltip / Separator /
Dialog / Popover / Switch / Tabs / Toast / RowButton / SearchField / GlyphSpinner /
cn / haptic / icons / useValue / useQuery / usePluginI18n`。

注意两处细节：
* `Button` 的 variant 是 `default / destructive / outline / secondary / chip /
  ghost / grip / floating / link / text / textStrong`，**没有 `muted`**（那是 Badge 的）
* UI 必须用 `jsx()` / `jsxs()` 调用，不能用 JSX 语法（加载时不经编译）

### 坑 J：图标"太小"

不是尺寸问题（官方 `fnos-apps` 只用 `64.png`），而是两点：

1. 原图 1254×1254，但内容边界只有 **817×776** —— 四周留白约 40%，
   缩到 64px 后实际图形只剩约 42px
2. Hermes 标志是中性灰线稿 + 透明底，放在深色桌面上对比度极低

处理：按 `getbbox()` 裁到内容边界 → 放到浅色圆角底（内边距 2%）→
**内容占比 96%**。翅膀、肩甲、权杖在小尺寸下都能辨认。

---

## 五、构建期踩坑

这些是打包与安装阶段的问题，都已固化进 `scripts/build.sh` 与 `packaging/fpk/cmd/`。

| # | 问题 | 处理 |
|---|---|---|
| 1 | PyPI 上的版本没有 `webapp` 子命令 | 必须从源码安装，不能只装 PyPI 包 |
| 2 | 源码要求 **Python 3.14**，不是 3.11 | 用 uv 建 3.14 venv；3.12 仅作回退 |
| 3 | uv 的 `exclude-newer` + 国内镜像 = 依赖全被过滤 | 走官方 pm 安装（自带哈希锁定） |
| 4 | NAS 直连 github.com 不稳定 | 构建在 e5 完成，只把产物传到 NAS |
| 5 | uv 的下载 URL 不能手工拼 | 用 uv 自身命令，不构造 URL |
| 6 | 安装阶段拿不到 `TRIM_SERVICE_PORT` | 以 manifest 的 `service_port` 兜底 |
| 7 | fnOS 的 `wizard/*` 必须是 JSON 数组 | 严格按数组结构生成 |
| 8 | `webapp --status` 报 "off" 是假象 | 以端口是否监听为准，不看该状态 |

构建需要 **Node ≥ 26**（官方 build 脚本要求）。e5 上备有 `/tmp/node26` 与
`/tmp/extra-libs`（glibc 补丁），完整命令见 `scripts/build.sh`。

---

## 六、交付物

### fpk 结构

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
├── ICON.PNG / ICON_256.PNG
└── manifest
```

### 安装时生成的 `config.yaml`

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

`install_callback` 用 venv 里的 `hash_password` 现场生成哈希；生成失败时
自动回退 `host: 127.0.0.1`，保证应用至少能起来。

---

## 七、已知限制

### 桌面内嵌的三个前提

内嵌入口要真正可用，需同时满足：

1. **网关透传 `Upgrade: websocket`** —— 部分 fnOS 版本不支持
2. **代理层把前端路由挂到子路径** —— 已通过改写 `__HERMES_BASE_PATH__` 解决
3. **WS 端点接受的会话凭证在子路径下仍有效** —— 实测上游 WS 端点即便直连也要求凭证

第 1、3 项依赖飞牛网关实现，本项目无法完全控制。若提示
`Desktop IPC bridge is unavailable`，**改用网页入口**，它不经过网关，功能完整。

### 浏览器形态相比 Electron 少了什么

- 无系统托盘 / 原生通知
- 无本地文件选择器的原生路径（走浏览器上传）
- 无自动更新（由 fpk 版本管理）
- 无桌面级硬件加速集成

**Agent 核心能力（工具调用、代码执行、文件读写、定时任务、多 Profile、
技能、MCP、持久记忆）全部保留。**

### 安全提醒

登录后等同于授予本机命令执行权限。**切勿直接暴露到公网**；
外网访问请用 VPN / Tailscale，或反代 + HTTPS。

---

## 八、排障速查

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

---

## 附录 A：内置飞牛 Skill

应用内置**飞牛官方 fnOS Skill**（`trim-cli` + 知识文档），装完 Agent 即具备
操作本机 NAS 的能力：文件、Docker、应用中心、存储与 RAID、相册、日志监控、
网络电源、用户权限、下载任务。

**需要一次 OAuth 授权**（Authorization Code + PKCE，Agent 不能代你确认）：

```bash
/var/apps/hermes-desktop/var/skills/fnos-trim/scripts/trim-cli login --no-open
```

命令打印 `http://<NAS_IP>:5666/signin?...` 链接，在浏览器确认授权后把返回的
授权码粘回命令提示即可。

> 踩坑：`postinstall` 阶段写入 Skill 存在覆盖竞态，已改为安装后置步骤。

## 附录 B：与官方安装脚本的关系

官方 `install.sh` 假设系统级 Python 与可写 `/usr/local`，与 fpk 沙箱模型不兼容。
本项目按官方脚本的**阶段划分**重新实现，但不复用其脚本：

1. `uv` 建 Python 3.14 venv
2. 官方 `pm` 安装依赖（哈希锁定 + 自动剥离 `exclude-newer`）
3. `pm` 不可用时回退到直接 `pip install`

不使用官方 `hermes setup`（交互式，且在 fpk 里会写入错误的 HOME）与
`hermes gateway install`（面向 systemd，而 fnOS 用自己的生命周期管理）。

---

## 附：核实来源

| 结论 | 依据 |
|---|---|
| Webapp 与 Dashboard 共用传输层 | `src/hermes_cli/webapp.py` 模块文档 |
| 非回环绑定强制认证 | `src/hermes_cli/web_server_boundary.py: should_require_auth()` |
| 白名单只管 Host 校验 | 同文件 `_is_accepted_host()` |
| 认证要求 scrypt 哈希 | 运行时日志提示 + `plugins/dashboard_auth/basic.py` |
| 子路径基准变量 | 上游注入的 `window.__HERMES_BASE_PATH__` |
| 官方应用入口惯例 | `conversun/fnos-apps` 全量 `ui/config` 抽样 |
