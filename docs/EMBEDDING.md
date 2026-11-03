# 桌面内嵌链路与薄适配层契约（EMBEDDING）

> **决策源**：[REFACTOR-CONTRACT.md](./REFACTOR-CONTRACT.md)（冻结契约）。本文解释「为什么这样设计 + 怎么验证/排障」。
> **一句话**：飞牛桌面 iframe → fnOS 统一网关 `:5666` → 应用 Unix Socket `target/app.sock` →
> **薄 adapter（只改 2 个安全头、body 零改写）** → `127.0.0.1:9120` 上游 Hermes webapp。
> 目录/文件布局见 [APP-STRUCTURE.md](./APP-STRUCTURE.md)。

---

## 1. 链路总览

```text
 飞牛桌面 (fnOS Web :5666)
   │  <iframe src="http://<nas>:5666/app/hermes-desktop/">
   │  同源 ⇒ SameSite=lax 的 fnOS 会话 Cookie 会随请求发送
   ▼
 fnOS 统一网关  /app/hermes-desktop/*
   │  先校验 NAS 登录态，再转发（官方 gateway 行为）
   ▼
 Unix Socket：/var/apps/hermes-desktop/target/app.sock
   │  监听者：app/runtime/gateway/gateway_adapter.py
   │  · 剥掉 /app/hermes-desktop 前缀（严格边界）
   │  · 注入 X-Forwarded-Prefix: /app/hermes-desktop
   │  · Host → 127.0.0.1:9120；Origin/Referer → http://127.0.0.1:9120
   │  · CSP: frame-ancestors 'none' → 'self'；X-Frame-Options: DENY → SAMEORIGIN
   ▼
 127.0.0.1:9120  hermes gateway（官方 webapp 渲染层）
   │  Agent / 工具 / 会话 / 技能 / 定时任务
   ▼
 浏览器直连入口（另一个图标）：http://<nas>:9120 —— 不经过 adapter，CSP 保持 'none'
```

要点：

- **内嵌窗口必须与飞牛桌面同源**（`:5666`）。iframe 直指 `:9120` 是跨源，`SameSite=lax` 的会话 Cookie 不会发送，表现就是反复登录/空白。
- 网关只保证「已登录 NAS」，**不负责应用业务鉴权**；应用侧的认证仍由上游 `dashboard.basic_auth` 把关。
- 直连入口与内嵌入口访问同一后端、同一份会话，区别只在「是否经过 adapter」。

官方依据（摘要）：[统一网关](https://developer.fnnas.com/docs/core-concepts/gateway-registration/) 规定了
`gatewayPrefix`/`gatewaySocket` 语义、请求先校验会话再转发、以及 `X-Trim-Userid` 等用户 Header；
[应用入口](https://developer.fnnas.com/docs/core-concepts/app-entry/) 规定了入入口字段语义。

---

## 2. 根因：上游 CSP 禁止被 iframe

### 2.1 事实

上游 commit **`8d91e48e`**（仓库 `NousResearch/hermes-agent`，文件 `hermes_cli/webapp_page_policy.py`，链接：
<https://github.com/NousResearch/hermes-agent/commit/8d91e48e>）为 webapp 页面统一下发 CSP，其中包含：

| CSP 指令 | 影响 |
|---|---|
| `frame-ancestors 'none'` | **任何页面都不允许 iframe 它**，飞牛桌面内嵌直接被浏览器拒绝 |
| 内联脚本 **sha256 白名单** | 页面里的内联 `<script>` 必须与哈希逐一匹配，**改动 HTML 字节即失效** |
| `base-uri 'none'` | 禁止 `<base>` 标签 |
| `form-action 'none'` | 表单不允许外提交 |

### 2.2 两个推论（决定了整个方案）

1. **不能改 body**。旧方案 `gateway_proxy.py` 往 HTML 里注入桥接脚本、改写 CSS/JS 里的站点根绝对路径 ——
   HTML 字节一变，sha256 白名单对不上，浏览器拒绝执行内联脚本 → 白屏/按钮全失效。
   因此新契约规定：**adapter 不得改写 HTML/CSS/JS，不得注入脚本、不得改 `<base>`**。
2. **只需放宽 frame 限制**。内嵌被拒的**唯一**原因就是 `frame-ancestors 'none'`；
   把它换成 `'self'`（即允许 fnOS 自己的 `:5666` 页面嵌入），其余 CSP 指令与 sha256 列表原样保留即可。

### 2.3 端点差异（契约冻结）

| 响应经过 | `Content-Security-Policy` 的 frame 指令 | `X-Frame-Options` | 预期 |
|---|---|---|---|
| 直连 `127.0.0.1:9120` / 局域网 `:9120` | `frame-ancestors 'none'` | `DENY` | 保持最严，任何第三方不能内嵌 |
| 经 adapter（Unix Socket） | `frame-ancestors 'self'` | `SAMEORIGIN` | 只允许同源（飞牛桌面）内嵌 |

> 安全边界：**只允许 `'self'`，禁止改成 `*` 或 `http://*`**；`X-Frame-Options` 同步放开到 `SAMEORIGIN`，
> 不得设为 `ALLOWALL`/省略。

---

## 3. 为什么前缀交给上游：`X-Forwarded-Prefix`

上游原生支持 `X-Forwarded-Prefix`（契约 §2 的已验证结论，引入版本未单独考据）。adapter 只要把该头发给上游，
**下面这些都由上游自己算对**（契约 §2）：

| 上游处理项 | 说明 |
|---|---|
| 静态资源 URL | `assets/*.js` 等自动带 `/app/hermes-desktop` 前缀 |
| `__HERMES_BASE_PATH__` | 前端路由基路径注入为前缀，`location.href = basePath + "/"` 不再跳回站点根 |
| 登录跳转 | `302 /login` 变成 `/app/hermes-desktop/login` |
| Cookie Path | 会话 Cookie 的 `Path` 落在前缀下 |

因此 adapter **不再**处理 `Location`、`Set-Cookie`（旧方案手改这两处，正是「登录循环/跳回桌面根」的来源）。
排障时若出现前缀相关症状，第一反应应是「`X-Forwarded-Prefix` 有没有送到上游 / 值对不对」，
而不是去改上游返回的 HTML。

历史教训（`docs/IMPLEMENTATION.md` D/E 条）仍适用：

- 前缀缺失 → 前端跳转落到 `http://<nas>:5666/`（飞牛桌面根）。
- `webapp-launch` 返回 `sign_in` 时那条 URL 是「请先登录」的根地址，拼接前缀会 302 到自己造成无限重定向；
  正确做法是让上游自己 302 到 `/login`（adapter 不改 Location 就自然正确）。

---

## 4. 薄 adapter 契约（`app/runtime/gateway/gateway_adapter.py`）

### 4.1 CLI（契约 §5）

```bash
python3 gateway_adapter.py \
  --socket /var/apps/hermes-desktop/target/app.sock \
  --upstream 127.0.0.1:9120 \
  --prefix /app/hermes-desktop \
  --health-path /.gateway-health \
  --log-file /var/apps/hermes-desktop/var/logs/adapter.log
```

`--health-path`、`--log-file` 可选；`--prefix` 与 socket 名必须来自 `gateway.conf`（单一来源，见 APP-STRUCTURE §4.7），
不得写死在 adapter 或 `cmd/main` 里。

**当前实现状态（2026-11-03 实测）**：`app/runtime/gateway/gateway_adapter.py` 已提供
`--socket/--upstream/--prefix/--health-path`（另含 `--version`）；契约中的 `--log-file` **尚未实现**，
日志暂由 `cmd/main` 重定向到 `TRIM_PKGVAR` 下的日志文件。响应头改写已确认为「只动 `frame-ancestors`」
（其余 CSP 指令与 sha256 白名单原样）。

### 4.2 行为矩阵

| 环节 | 必须做 | 必须不做 |
|---|---|---|
| 入站路径 | 严格剥掉 `--prefix`；剥离后以 `/` 开头；路径不在前缀下 → 404 | 宽松前缀匹配（如 `/app/hermes-desktopX` 也算命中） |
| 请求头 | 注入/覆盖 `X-Forwarded-Prefix`；`Host` 指向上游；`Origin`/`Referer` 重写为 `http://127.0.0.1:<port>`；透传 `Cookie`、`X-Trim-*`、`Authorization`、`Sec-WebSocket-*` | 透传 hop-by-hop 头（`Connection`/`Transfer-Encoding`/`Upgrade` 等按 RFC 处理） |
| 请求体 | chunked 要解码后转发；有大小上限与超时 | 缓冲整个请求体到内存无上限 |
| 响应头 | **只改** CSP 的 `frame-ancestors` 与 `X-Frame-Options`；其余头原样 | 改 `Location`、`Set-Cookie`、`Content-Length` 等业务头 |
| 响应体 | **零改写**（字节透传），支持流式（SSE/大文件）；HEAD/204/304 无 body | 改 HTML/CSS/JS、注入脚本、改 `<base>`、缓冲整个响应 |
| 连接 | 超时（连接/读/写）、健康端点、SIGTERM 时关闭并清理 socket | 泄漏 socket 文件（重启前应可 `rm -f` 幂等） |
| WebSocket | 单一 WS 路径；握手头转发 + `Origin/Referer` 重写；`101` 后帧双向透传 | 把 WS 当普通 HTTP 用 `http.client` 转发（读完整响应就结束，扛不住帧流） |

**纯标准库**实现，可被 `python3 -m unittest` 直接 import 测试（不依赖第三方包、不依赖 Hermes 源码）。

### 4.3 禁止项（契约 §5，违反即回退到旧方案的坑）

```text
× 改写 HTML / CSS / JS
× 注入任何脚本或注释
× 修改 <base>
× 修改 Location
× 修改 Set-Cookie
× 缓冲整个响应（破坏 SSE/大文件）
× 额外监听 TCP 端口（见 §5）
```

---

## 5. 安全边界

| # | 边界 | 要求 | 违反后果 |
|---|---|---|---|
| 1 | **CSP 只放开到 `'self'`** | `frame-ancestors` 由 `'none'` 改为 `'self'`；`X-Frame-Options` 由 `DENY` 改为 `SAMEORIGIN`；其余 CSP 指令与 sha256 白名单原样 | 改成 `*` 会允许任意站点内嵌并驱动 Agent；漏改则内嵌被浏览器拒绝 |
| 2 | **只走 Unix Socket** | adapter 只监听 `target/app.sock` | 旧 `gateway_proxy.py` 曾额外监听 `0.0.0.0:9121` 且「Host 保持回环地址即免登录」——局域网任何人都能绕过认证操作 Agent。**不得重新引入任何 TCP 监听** |
| 3 | **不碰凭据** | 不读/不写 Cookie、`TRIM_API_TOKEN`、密码；日志只记状态码/路径/耗时 | 凭据进日志或报告 = 泄漏 |
| 4 | **登录态 ≠ 业务鉴权** | 网关只保证 NAS 已登录；应用自身仍靠上游 auth gate（`dashboard.basic_auth`，scrypt hash） | 误以为「网关放行=有权限」会做出越权接口 |
| 5 | **上游认证门不可绕过** | 上游按**绑定地址**决定是否强制认证（`0.0.0.0` 必须配 auth provider，否则拒绝启动 webapp）；adapter 不能靠伪造 `Host` 绕过 | 表现为「进程活着、端口不监听 → 内嵌 502」，日志只有一句 ERROR |
| 6 | **前缀注入不可信输入化** | `Origin/Referer` 只重写为固定上游地址；不把客户端 `Origin` 反射回上游；`X-Trim-*` 原样透传、不解析 | 反射/信任客户端头 → CSRF 面扩大 |
| 7 | **不在 adapter 做鉴权/重定向** | 302 由上游产生；adapter 只转发 | 自定义重定向极易造成登录循环 |

> 官方对网关的表述是「先校验会话，再把用户信息通过 Header 转发；应用仍需负责自己的业务鉴权」
> （[统一网关](https://developer.fnnas.com/docs/core-concepts/gateway-registration/)）。
> 本应用将业务鉴权交给上游 basic_auth，不在 adapter 重复实现。

---

## 6. 验证命令（在 e5 上执行，可复制）

```bash
# ── 0) 进程与 socket 就位 ───────────────────────────────────────────────
pgrep -af gateway_adapter.py
ss -lx | grep app.sock
ss -ltnp | grep -E ':9120\b'

# ── 1) 经 adapter 的 CSP（期望 self / SAMEORIGIN）────────────────────────
curl -sSI --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/ \
  | grep -iE '^(HTTP/|content-security-policy:|x-frame-options:)'

# ── 2) 直连端的 CSP（期望保持 none / DENY）─────────────────────────────
curl -sSI http://127.0.0.1:9120/ \
  | grep -iE '^(HTTP/|content-security-policy:|x-frame-options:)'

# ── 3) 网关入口（真机 5666；需要 NAS 登录会话）──────────────────────────
curl -sSI 'http://192.168.1.2:5666/app/hermes-desktop/' \
  | grep -iE '^(HTTP/|location:|set-cookie:|content-security-policy:)' | head -n 12

# ── 4) 健康端点（adapter 自身）──────────────────────────────────────────
curl -sS --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/.gateway-health ; echo

# ── 5) WebSocket 握手（<WS_PATH> 取浏览器 Network → WS 里实际路径）───────
curl -sSI --unix-socket /var/apps/hermes-desktop/target/app.sock \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  "http://localhost/app/hermes-desktop<WS_PATH>" | head -n 5
# 期望：HTTP/1.1 101 Switching Protocols

# ── 6) body 零改写（对照：同样带 X-Forwarded-Prefix 直连上游）───────────
curl -s -H 'X-Forwarded-Prefix: /app/hermes-desktop' http://127.0.0.1:9120/ -o /tmp/upstream.html
curl -s --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/ -o /tmp/adapter.html
sha256sum /tmp/upstream.html /tmp/adapter.html
# 期望：两个哈希相同（若未登录导致 302 空体，先带 Cookie 再比对）
```

> 契约 §6 的验收命令保持有效：
> `curl --unix-socket /var/apps/hermes-desktop/target/app.sock http://localhost/app/hermes-desktop/`、
> `curl -sSI http://127.0.0.1:9120/`、网关 `:5666/app/hermes-desktop`、WS 101。

---

## 7. 排障矩阵

| 现象 | 一眼判定 | 常见根因 | 处置 |
|---|---|---|---|
| **CSP：iframe 被拒**（控制台 `frame-ancestors 'none'`） | 上面命令 1 的 `content-security-policy` 仍是 `'none'` | adapter 未跑 / 未经过 adapter（iframe 直指 9120）/ 改错头（改了别的头或漏改） | 确认 iframe URL 是 `/app/hermes-desktop`；确认 adapter 进程与 socket；检查 adapter 只替换 `frame-ancestors` 值 |
| **白屏但 HTTP 200** | 命令 6 两个哈希不同 | body 被改写（注入了脚本/改了 HTML）→ CSP 内联 sha256 失效 | 回退 adapter 为字节透传；删除任何 HTML/CSS/JS 改写逻辑 |
| **502 / Bad Gateway** | `pgrep -af gateway_adapter.py` 无进程，或 `ss -lx \| grep app.sock` 无结果 | adapter 挂了 / socket 未建立（启动即崩）/ 上游未监听 | 看 `cmd/main` 重定向的日志（当前 adapter 无 `--log-file`）与 `var/info.log`；`cmd/main` 会因 socket 未出现而失败；先修 adapter 启动错误 |
| **网关入口 502，但直连 9120 正常** | 命令 2 通、命令 1 不通 | adapter 未启动或 socket 路径与 `gatewaySocket` 不一致（三方不一致） | 对齐 `gateway.conf`/`app/ui/config`/`cmd/main`；确认 socket 在 `target/app.sock` |
| **直连 9120 也不通，进程活着** | `ss -ltnp \| grep :9120` 无监听 | 上游认证门：绑 `0.0.0.0` 但缺 `dashboard.basic_auth`，webapp 拒绝启动 | 按 APP-STRUCTURE §4.5 写入 `password_hash`；看 `var/logs/gateway.log` |
| **WS 403 / `Desktop IPC bridge is unavailable`** | 命令 5 返回 `403` | 握手头未正确重写：`Host` 非上游、`Origin/Referer` 仍是 `:5666`，被上游拒绝 | adapter 握手前重写 `Host/Origin/Referer`；透传 `Sec-WebSocket-*`；确认走裸 socket 双向转发而非 `http.client` |
| **WS 101 但页面仍报桥不可用** | 101 后无帧流动 / 只单向 | 握手后未双向透传帧，或缓冲导致丢帧 | 101 后进入双向 relay（含二进制帧）；检查连接超时设置 |
| **登录循环 / 跳回 `/login`** | 命令 3 的 `location` 不带 `/app/hermes-desktop` 前缀；或 `set-cookie` 的 `Path` 不在前缀下 | `X-Forwarded-Prefix` 未注入/值错；或 adapter 又手动改了 `Location`/`Set-Cookie` | 确认注入头且 prefix 与 `gateway.conf` 一致；删掉对 Location/Set-Cookie 的任何改写 |
| **跳回飞牛桌面根（`:5666/`）** | 页面源码 `__HERMES_BASE_PATH__` 为空或 `/` | 同上：前缀没到上游 | 同上；不要靠改 HTML 兜底 |
| **静态资源 404（`/assets/...`）** | 资源请求 URL 没有 `/app/hermes-desktop` 前缀 | 前缀未注入 → 上游按站点根生成资源 URL | 修前缀注入；不要在前端手拼前缀 |
| **无限 302 到自己** | `location` 以 `/app/hermes-desktop/` 指向登录相关路径 | 旧方案把 `webapp-launch` 的 `sign_in` 根地址拼了前缀 | 交给上游 302；adapter 不改 Location |
| **重启后 502 / socket 残留** | `ls -l target/app.sock` 存在但无进程 | 上次异常退出未清理 socket | adapter 启动前 `rm -f` 幂等重建；`cmd/main stop` 负责清理 |
| **日志报 `A gateway already owns this host`** | 上游拒绝启动 | 残留 Hermes 进程占用 host | `cmd/main stop` 清理；启动用 `--replace` 语义（现有实现） |

---

## 8. 旧方案为何失败（回归检查：这些行为不得复活）

| 旧实现（已删除） | 失败机理 | 新契约 |
|---|---|---|
| `rewrite_html` / `rewrite_css` / `rewrite_javascript` + 注入桥接脚本 | 改动响应体 → CSP 内联脚本 sha256 不匹配 → 白屏 | body 零改写 |
| `_rewrite_location` / `_rewrite_set_cookie` | 手工拼前缀与 Cookie Path，和上游自身的前缀逻辑叠加 → 登录循环 | 交给上游 `X-Forwarded-Prefix` |
| `--tcp-listen 0.0.0.0:9121`（Host 回环即免登录） | 局域网可绕过认证操作 Agent | 只监听 Unix Socket，无额外 TCP 端口 |
| 不处理 CSP | `frame-ancestors 'none'` → iframe 被拒 | 仅改 frame 指令为 `'self'` |
| 用 `http.client` 转发 WS | 读完一个响应就结束，扛不住帧流 | 裸 socket 双向 relay |

---

## 9. 回归不变量（供 `scripts/check_gateway_contract.py` / 单测引用）

1. `gateway_adapter.py` 只 import 标准库；可被 `python3 -m unittest` import。
2. `--prefix` 与 socket 名来自 `gateway.conf`；`app/ui/config` 的 `gatewayPrefix`/`gatewaySocket` 与之相等。
3. 入站路径不在前缀下 → 404，不得误放行相似前缀。
4. 响应 body 与「上游在相同 `X-Forwarded-Prefix` 下产出的 body」逐字节一致。
5. 仅 `Content-Security-Policy` 的 frame 指令与 `X-Frame-Options` 被修改；`Location`/`Set-Cookie` 原样。
6. 直连 9120 的 CSP 仍为 `frame-ancestors 'none'` / `DENY`。
7. 无任何 TCP 监听；只有 `target/app.sock`。
8. 日志不含 Cookie、`Authorization`、`TRIM_API_TOKEN`、密码。
9. SIGTERM 后 socket 文件被清理；重复启动幂等。

---

## 10. 未决点

| # | 问题 | 现状 |
|---|---|---|
| 1 | WS 单一路径的具体值 | 契约只要求「单一路径」未命名；以上游前端实际连接路径为准，adapter 应可配置或按升级请求原样转发 |
| 2 | `--health-path` 是否经 fnOS 网关可达 | 当前默认 `/.gateway-health` 落在前缀外，网关只转发前缀路径 → 该端点用于本机/套接字排障 |
| 3 | 内嵌入口何时从「实验性」转正 | 取决于真机验收（CSP/WS/登录循环三项全绿）；未验收前保留「网页入口推荐」的产品措辞 |
| 4 | 上游换 commit 后 CSP/前缀行为是否稳定 | 以 `hermes_cli/webapp_page_policy.py` 为准；升级上游时重跑 §6 的 1/2/6 三项检查 |
| 5 | adapter PID 文件命名 | 契约未冻结（见 APP-STRUCTURE §8），实现前需 Lead 更新契约 |
| 6 | adapter `--log-file` | 契约 §5 可选参数，当前实现未提供；补齐前以 `cmd/main` 重定向为准 |
