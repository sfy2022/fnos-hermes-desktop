# tests/browser — 桌面内嵌人工验收夹具

本目录服务 `docs/REFACTOR-CONTRACT.md` §2/§5/§6 的**人工浏览器验收**：
确认「飞牛桌面 → iframe → 统一网关 → `target/app.sock` → `gateway_adapter.py` →
`127.0.0.1:9120`」这条链路里，浏览器**没有**因 CSP 拒绝内嵌，并且 WebSocket 握手返回 **101**。

> 这里的东西只用于人工排障/验收：不进 fpk 包、不参与运行时、不被 CI 自动执行。
> 自动化契约测试在 `tests/gateway/`（QA 负责）。

## 文件

| 文件 | 作用 |
|---|---|
| `fake-gateway.html` | 模拟飞牛桌面父页面 + 一个应用窗口 iframe；支持 `?url=` 指定 iframe 地址、`?ws=` 预填 WebSocket 探测地址；内置状态面板、事件日志、CSP 违规监听、WS 探测、桌面/平板/手机视口切换 |
| `proxy.py` | 纯标准库本地反向代理，让夹具页面与 iframe **同源**（CSP 判定的前提）；转发 HTTP 与 WebSocket，改写 `Host`/`Origin`/`Referer`，处理 `Location`/`Set-Cookie` |
| `README.md` | 本文件 |

## 为什么必须同源

适配层只改一个响应头：把 CSP 里的 `frame-ancestors 'none'` 放宽为 `frame-ancestors 'self'`
（`X-Frame-Options: DENY` → `SAMEORIGIN`）。`'self'` 的含义是**父页面必须与响应文档同源**。
因此：

- 在真实飞牛桌面里验收：父页面就是飞牛桌面本身，与网关同源 —— 这是**最终口径**（方式 A）。
- 本地直接双击 `fake-gateway.html`（`file://`）或另起端口打开时，父页面跨源，
  正确的 `'self'` 也会拒绝内嵌；Console 里的 `Refused to display` **不能**判为适配层故障。
  要本地验证 CSP，用方式 B 的同源回放。

## 方式 A：真实飞牛桌面（最终验收）

前提：FPK 已安装到 e5（`packaging/fpk` 构建产物），`cmd/main start` 已启动，
网关已注册 `/app/hermes-desktop`，`target/app.sock` 存在。

1. 浏览器打开飞牛桌面：`http://192.168.1.2:5666/`（局域网；远程用 HTTPS 5667/443）。
2. 点击桌面图标 **Hermes Desktop (桌面内嵌)**。
3. 按 `F12` 打开 DevTools，切到 **Console** 与 **Network**，逐项对照下方「检查项」。
4. 证据留存：Console 截图、Network 中 WS 请求的 101 截图、iframe 文档响应头截图
   （`content-security-policy` / `x-frame-options`），附到验收报告。

> 直连图标 **Hermes Desktop**（`:9120`）是另一种入口，CSP 保持 `'none'`，
> 不经适配层，**不适用**本夹具的判定。

## 方式 B：本地同源回放（不依赖真机桌面）

```powershell
# 仓库根目录执行；--target 换成你的设备地址
python tests/browser/proxy.py --target http://192.168.1.2:5666

# 浏览器打开（注意端口与 proxy.py 默认 8090 一致）
# http://127.0.0.1:8090/tests/browser/fake-gateway.html?url=/app/hermes-desktop
```

- 远程 HTTPS 设备：`python tests/browser/proxy.py --target https://192.168.1.2:5667 --insecure`
- 若系统里 `python` 不可用，可换用 `py -3`（Windows）或本机 Python 3.8+ 的绝对路径；
  脚本只用标准库，无第三方依赖。
- 代理只监听 `127.0.0.1`（默认），不会暴露到局域网；`Ctrl+C` 停止。
- 夹具支持 `?url=`：可指向任意前缀，例如
  `...fake-gateway.html?url=/app/hermes-desktop`、
  `...fake-gateway.html?url=http%3A%2F%2F192.168.1.2%3A9120%2F`
  （后者是跨源对照，预期被拒，仅用于确认浏览器行为）。
- `?ws=` 可预填 WS 探测输入框；不确定端点时留空，直接看 DevTools Network 即可。
- 若 iframe 内出现飞牛登录页，按提示在该窗口登录（凭证经代理转发到设备，代理不落盘）。
- 登录后若直接跳到 `http://192.168.1.2:5666/...`（设备地址而非 `127.0.0.1:8090`），
  说明上游有未改写的绝对跳转，属已知代理局限：记录该 URL 与 Network 截图后交 Lead。

## 检查项（人工，逐条打勾）

| # | 位置 | 通过标准 |
|---|---|---|
| 1 | DevTools → Console | **没有** CSP 拒绝：无 `Refused to display`、无 `frame-ancestors` 违规、无 `violates the following Content Security Policy` |
| 2 | DevTools → Network → 过滤 `WS` | 应用自身的 WebSocket 请求状态为 `101 Switching Protocols`（不是 403/404/502/pending） |
| 3 | iframe 显示 | 出现 Hermes Desktop 界面或登录页，而不是持续白屏/纯错误页 |
| 4 | Network → iframe 文档响应头 | `content-security-policy` 含 `frame-ancestors 'self'`（不是 `'none'`）；`x-frame-options: SAMEORIGIN` |
| 5 | 夹具事件日志 | 出现 `iframe load 事件触发`；无 `securitypolicyviolation`（夹具文档自身无 CSP，正常不出现） |

> 1 与 3/4 是同一件事的两面：CSP 仍是 `'none'` → 浏览器拒绝 → 白屏 + Console 报错。
> 若 Console 干净但 iframe 白屏，先查 502/网关路由（见下表），不要误判为 CSP。

## 失败签名速查

| 现象 | 最可能原因 | 下一步 |
|---|---|---|
| 白屏 + Console `Refused to display ... frame-ancestors 'none'` | 请求没走适配层（例如浏览器直接命中 `:9120`，或旧 `gateway_proxy.py` 逻辑仍在） | 核对 `app/ui/config` 的 `gatewayPrefix`/`url` 与 `gateway.conf`；确认 `cmd/main` 启动的是新 adapter；`curl --unix-socket` 验证 |
| iframe 502 / 空白 + 夹具日志无 load | adapter 未启动、`app.sock` 不存在、`127.0.0.1:9120` 未监听 | `cmd/main status`；看应用日志中 adapter/网关日志；重启后重试 |
| WS 403/404/502 或握手失败 | 网关未透传 `Upgrade`，或 WS 路由不在 `gatewayPrefix` 下 | 检查 adapter WS 分支与网关路由；确认 WS 路径以 `/app/hermes-desktop` 开头 |
| iframe 弹出 Basic Auth / 401 | 上游 `basic_auth` 与网关登录态的衔接问题 | 记录现象与响应头，交 Lead 决策；不要在前端夹具里绕过鉴权 |
| 大量静态资源 404 | adapter 未注入 `X-Forwarded-Prefix`，或前缀剥离边界错误 | 检查 adapter 注入头与 `strip_prefix`；对比 iframe 内资源 URL |
| 登录后跳回设备 IP、脱离 `127.0.0.1:8090` | 上游存在未改写的绝对跳转（代理无法改写响应体） | 留存 Network 证据；改用方式 A 复核真实桌面，再决定是否需要 adapter 侧处理 |

## 安全与边界

- `proxy.py` 只监听 `127.0.0.1`，仅用于本机人工验证；**不要**加 `--listen 0.0.0.0`，不要部署到服务器。
- 夹具与代理不读取、不记录账号密码、Cookie、`TRIM_API_TOKEN`；不要把它们贴进报告或 Issue。
- `fake-gateway.html` 完全静态、无外部网络资源、不调用任何开放 API；iframe 内容来自设备网关本身。
- 代理会移除 `Set-Cookie` 的 `Domain`/`Secure` 属性（`--no-relax-cookies` 可关闭），仅作用于本地回放，
  不代表生产行为；生产链路的 Cookie 由上游 + 网关正常处理。
- 本目录不属于 `packaging/fpk/**`，不会被 fnpack 打进安装包。

## 与自动化测试的分工

- `tests/gateway/`（QA）：adapter 的契约级单测（CSP 只改 `frame-ancestors`、响应体零改写、WS 透传等），可在 CI/无浏览器环境执行。
- 本目录：需要**真实浏览器**才能观察的 CSP 执行与 DevTools 101，无法自动化；结果以截图/录屏留证。
