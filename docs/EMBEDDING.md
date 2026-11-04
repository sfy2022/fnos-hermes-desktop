# 桌面内嵌链路与薄适配层（EMBEDDING）

> 相关文档：[当前状态](./CURRENT-STATE.md)、[包结构](./APP-STRUCTURE.md)、
> [冻结契约](./REFACTOR-CONTRACT.md)。

## 1. 链路总览

```text
飞牛桌面
  │  <iframe src="/app/hermes-desktop/">
  ▼
fnOS 统一网关  /app/hermes-desktop/*
  │  先校验 NAS 登录态，再转发
  ▼
Unix Socket：/var/apps/hermes-desktop/target/app.sock
  │  gateway_adapter.py
  │  · 剥掉 /app/hermes-desktop 前缀
  │  · 注入 X-Forwarded-Prefix: /app/hermes-desktop
  │  · Host → 127.0.0.1:9120，Origin/Referer 同步重写
  │  · CSP: frame-ancestors 'none' → 'self'
  │  · X-Frame-Options: DENY → SAMEORIGIN
  │  · HTTP 模式去掉认证 Cookie 的 Secure/__Secure-
  ▼
127.0.0.1:9120  Hermes webapp
  │
  ├── 直连入口：http://<NAS_IP>:9120（不经过 adapter，CSP 保持 'none'）
  └── Agent / 工具 / 会话 / 技能 / 定时任务
```

要点：

- 内嵌窗口必须与飞牛桌面同源，否则 `SameSite=lax` 的会话 Cookie 不会发送。
- 网关只校验 NAS 登录态，不负责 Hermes 业务鉴权；业务鉴权仍由上游
  `dashboard.basic_auth` 负责。
- 桌面只声明一个内嵌入口；直连 `:9120` 不创建第二个入口，两者访问同一后端和同一份会话。
- 内嵌登录需要 Hermes 账号，但**不需要先直连 `:9120` 登录一次**。

---

## 2. 为什么必须放宽 CSP

上游 Webapp 页面的 CSP 包含：

| 指令 | 影响 |
|---|---|
| `frame-ancestors 'none'` | 任何页面都不能 iframe 它，飞牛桌面直接拒绝 |
| 内联脚本 sha256 白名单 | 修改 HTML 字节会导致白名单失效 |
| `base-uri 'none'` | 禁止 `<base>` |
| `form-action 'none'` | 表单不允许外提交 |

因此：

1. **不能改写 body**。注入脚本、改 HTML/CSS/JS 会让内联脚本哈希不匹配，导致白屏。
2. **只放宽 frame 指令**。adapter 只把 `frame-ancestors` 改为 `'self'`，
   把 `X-Frame-Options` 改为 `SAMEORIGIN`，其余 CSP 指令和 sha256 列表保持原样。

| 响应路径 | `frame-ancestors` | `X-Frame-Options` |
|---|---|---|
| 直连 `:9120` | `'none'` | `DENY` |
| 经 adapter | `'self'` | `SAMEORIGIN` |

不得把 `frame-ancestors` 改成 `*`，也不得省略 `X-Frame-Options`。

---

## 3. 前缀交给上游：`X-Forwarded-Prefix`

上游原生支持 `X-Forwarded-Prefix`，adapter 只需要把该头发给上游：

| 上游处理项 | 结果 |
|---|---|
| 静态资源 URL | 自动带 `/app/hermes-desktop` 前缀 |
| `__HERMES_BASE_PATH__` | 前端路由基路径正确 |
| 登录跳转 | `302 /login` 变为 `/app/hermes-desktop/login` |
| Cookie Path | 会话 Cookie 落在前缀下 |

因此 adapter 不修改 `Location`，也不修改 Cookie value/Path。

唯一例外是 HTTP 网关的 Cookie 兼容：

- `gateway.conf` 设置 `forwarded_proto=http`；
- adapter 覆盖上游 `X-Forwarded-Proto`，并移除客户端传入的 `Forwarded`；
- 响应中的认证 Cookie 去掉 `Secure` 和 `__Secure-` 前缀，避免 HTTP 浏览器直接丢弃；
- Cookie 的 value、Path、`HttpOnly`、`SameSite`、`Max-Age` 保持不变。

如果网关是纯 HTTPS，可把 `forwarded_proto` 改为 `https`，恢复 Secure Cookie。

---

## 4. 适配层契约

### 4.1 启动参数

```bash
python3 gateway_adapter.py \
  --socket /var/apps/hermes-desktop/target/app.sock \
  --upstream 127.0.0.1:9120 \
  --prefix /app/hermes-desktop \
  --forwarded-proto http \
  --health-path /.gateway-health \
  --log-file /var/apps/hermes-desktop/var/logs/gateway-adapter.log
```

`--prefix` 和 socket 名来自 `gateway.conf`；不得在 adapter 或 `cmd/main` 中另写常量。
`--forwarded-proto` 可选值为 `auto`、`http`、`https`，默认值由 `gateway.conf` 提供。

### 4.2 行为矩阵

| 环节 | 必须做 | 禁止 |
|---|---|---|
| 入站路径 | 严格剥掉 prefix；路径不在前缀下 → 404 | 宽松前缀匹配 |
| 请求头 | 注入/覆盖 `X-Forwarded-Prefix`；`Host` 指向上游；重写 `Origin`/`Referer`；透传 Cookie、`X-Trim-*`、Authorization、WebSocket 头 | 透传 hop-by-hop 头 |
| 请求体 | 支持 chunked；有大小上限和超时 | 无上限缓冲 |
| 响应头 | 只改 CSP frame 指令、X-Frame-Options；HTTP 模式去掉 Cookie 的 Secure/`__Secure-` | 改 Location、Cookie value/Path、Content-Length |
| 响应体 | 字节透传，支持 SSE/大文件；HEAD/204/304 无 body | 改 HTML/CSS/JS、注入脚本、改 `<base>` |
| 连接 | 连接/读/写超时；健康端点；SIGTERM 清理 socket | 泄漏 socket |
| WebSocket | 握手头转发；101 后双向帧透传 | 用 `http.client` 读完一个响应 |

### 4.3 健康端点

```bash
curl --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/.gateway-health
```

返回 adapter 版本、PID、prefix、forwarded_proto、上游地址和上游可达性。
该端点只用于本机/套接字排障；正常网关入口不应依赖它。

---

## 5. 安全边界

| # | 边界 | 要求 |
|---|---|---|
| 1 | CSP 只放开到 `'self'` | 不得改成 `*`；XFO 只到 `SAMEORIGIN` |
| 2 | 只监听 Unix Socket | 不得新增 TCP 监听，不得用回环 Host 绕过认证 |
| 3 | 不读写凭据 | adapter 不记录 Cookie、Token、密码；日志只记状态码、路径、耗时 |
| 4 | 登录态 ≠ 业务鉴权 | 应用鉴权仍由上游 `dashboard.basic_auth` 负责 |
| 5 | 不绕过上游认证门 | 上游按绑定地址决定是否要求认证；adapter 不得伪造 Host 绕过 |
| 6 | 不反射客户端头 | `Origin`/`Referer` 只重写为固定上游地址；`X-Trim-*` 原样透传、不解析 |
| 7 | 不在 adapter 做鉴权/重定向 | 302 由上游产生，adapter 只转发 |

---

## 6. 排障

### 6.1 进程与监听

```bash
pgrep -af gateway_adapter.py
ss -lx | grep app.sock
ss -ltn | grep ':9120'
```

- `app.sock` 不存在：adapter 未启动或启动崩溃。
- `9120` 未监听：上游 webapp 未启动，常见原因是 `config.yaml` 缺少
  `dashboard.basic_auth`，或 `webapp.host/port` 配置错误。

### 6.2 CSP 仍然拒绝 iframe

```bash
curl -sSI --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/ \
  | grep -iE '^(HTTP/|content-security-policy:|x-frame-options:)'
```

如果仍是 `frame-ancestors 'none'`，说明请求没有经过 adapter，或 adapter 没有运行。
确认 iframe 地址是 `/app/hermes-desktop/`，且 `target/app.sock` 属于当前实例。

### 6.3 登录后回到 `/login`

按顺序检查：

1. 浏览器是否残留旧的 `hermes_*` Cookie（同名不同 Path）；
2. 响应头中的 Cookie 是否仍带 `Secure` 或 `__Secure-`；
3. `gateway.conf` 的 `forwarded_proto` 是否为 `http`；
4. 直接清除该 NAS 站点的 Cookie，再用无痕窗口登录。

旧 Cookie 被浏览器同时发送时，服务端可能读到过期会话，表现就是登录成功后仍回登录页。

### 6.4 WebSocket / IPC bridge 不可用

确认 adapter 转发 `Upgrade`/`Connection`/`Sec-WebSocket-*` 头，并在 101 后做双向帧转发，
而不是把它当普通 HTTP 响应读取。上游返回 403 时，检查握手阶段的 `Host`、`Origin`、
`Referer` 是否已改为上游地址。

### 6.5 静态资源 404 或白屏

- 确认入口 URL 带尾斜杠；
- 确认 `X-Forwarded-Prefix` 已注入且值为 `/app/hermes-desktop`；
- 如果响应 body 被改写，CSP 内联脚本哈希会失效，必须回退到字节透传。

---

## 7. 回归不变量

1. `gateway_adapter.py` 只使用标准库，可独立导入。
2. `--prefix` 和 socket 名来自 `gateway.conf`；`app/ui/config` 与之一致。
3. 入站路径不在前缀下时返回 404。
4. 响应 body 与上游在相同 `X-Forwarded-Prefix` 下产生的 body 逐字节一致。
5. 只修改 CSP frame 指令、XFO 和 HTTP 认证 Cookie 的 Secure/`__Secure-` 属性。
6. 直连 `:9120` 的 CSP 保持 `'none'` / `DENY`。
7. 不新增 TCP 监听，只有 `target/app.sock`。
8. 日志不包含 Cookie、Authorization、Token、密码。
9. SIGTERM 后清理 socket，重复启动幂等。
