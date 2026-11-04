# 设计与实施记录（IMPLEMENTATION）

> 本文是历史设计记录，保留关键决策和踩坑原因；当前版本、目录和鉴权以
> [CURRENT-STATE.md](./CURRENT-STATE.md)、[APP-STRUCTURE.md](./APP-STRUCTURE.md)、
> [EMBEDDING.md](./EMBEDDING.md) 为准。

## 1. 目标

把 Hermes 官方 Desktop 的 **Webapp bundle** 打包成 fnOS 应用，在 NAS 上运行官方 gateway，
通过浏览器使用原版 Desktop 工作区，不修改上游代码。

不使用 Electron，因为 fnOS 没有桌面显示环境；Webapp bundle 与 Dashboard 共用同一套传输层、
会话和工具。

## 2. 架构

```text
局域网浏览器 ──► http://<NAS_IP>:9120       Hermes basic_auth
飞牛桌面 iframe ─► /app/hermes-desktop/      fnOS 统一网关
                        │
                        ▼
                 target/app.sock
                        │  gateway_adapter.py
                        ▼
                 127.0.0.1:9120
                        │
                 Hermes webapp / Agent
```

- 网关只校验 NAS 登录态；Hermes 自己负责业务鉴权。
- adapter 只做前缀、头、CSP 和 HTTP Cookie 兼容，不改写响应体。
- 桌面只声明一个内嵌入口；直连 `:9120` 不创建第二个入口。

## 3. 关键决策

| 决策 | 原因 |
|---|---|
| 使用 Webapp bundle，不使用 Electron | 无桌面环境；官方 Webapp 与 Dashboard 共用传输层 |
| 绑定 `0.0.0.0:9120` 并强制认证 | 非回环绑定会触发上游认证门；缺少认证时 webapp 拒绝监听 |
| 内嵌入口走 fnOS 统一网关 | iframe 直连 `:9120` 是跨源，SameSite 会话 Cookie 不会发送 |
| 使用 `X-Forwarded-Prefix` | 上游自己处理资源 URL、路由基路径、登录跳转和 Cookie Path |
| 不改写 HTML/CSS/JS | CSP 内联脚本 sha256 白名单会对字节变化失效 |
| 只监听 Unix Socket | 避免额外 TCP 端口被用来绕过认证 |
| HTTP 模式放宽认证 Cookie | 上游按 HTTPS 下发 Secure Cookie 时，HTTP 浏览器会丢弃 |

## 4. 认证与绑定地址

上游根据 `webapp.host` 判断是否强制认证：

- 绑定回环地址：不强制；
- 绑定 `0.0.0.0`：必须配置认证提供方，否则 webapp 不监听。

本应用绑定 `0.0.0.0:9120`，因此安装时必须写入：

```yaml
dashboard:
  basic_auth:
    username: <用户名>
    password_hash: "scrypt$..."
```

`password_hash` 是 scrypt 哈希，不是明文。首装时如果 venv 尚未就绪，安装脚本会在依赖安装完成后
补充哈希，并在启动时自愈；否则会出现“进程活着、9120 不监听、外部 502”的静默失败。

## 5. 内嵌链路

1. 飞牛桌面加载 `/app/hermes-desktop/`；
2. 网关校验 NAS 登录态，把请求转发到 `target/app.sock`；
3. adapter 剥掉 prefix，把请求发往 `127.0.0.1:9120`；
4. 上游按 `X-Forwarded-Prefix` 生成正确的资源 URL、跳转和 Cookie Path；
5. 响应经过 adapter 时只调整 CSP/XFO 和 HTTP Cookie 属性。

### 5.1 为什么不能改 HTML

上游 CSP 使用内联脚本 sha256 白名单。旧方案通过注入脚本、改 `<base>` 或改资源路径解决子路径问题，
会让哈希不匹配，浏览器拒绝执行脚本，表现为白屏或按钮失效。

正确做法是让上游通过 `X-Forwarded-Prefix` 自己生成正确的页面，adapter 对 body 做字节透传。

### 5.2 为什么需要处理 Cookie

上游按 `request.url.scheme` 决定 Cookie 是否带 `Secure`。如果 fnOS 网关把上游请求标成 HTTPS，
而浏览器实际用 HTTP 打开页面，登录响应中的 `Secure` / `__Secure-` Cookie 会被浏览器丢弃，
登录后又被重定向回 `/login`。

`gateway.conf` 中的 `forwarded_proto=http` 会让 adapter 覆盖上游 scheme，并去掉认证 Cookie 的
Secure/`__Secure-` 属性；Cookie value 和 Path 不变。HTTPS 部署可改为 `https`。

## 6. 运行时注意点

### 6.1 数据目录属主

Hermes 以 `hermes-desktop` 用户运行。如果使用其他系统账号手动运行过 Hermes，会留下其他属主的
文件（session token、gateway lock、cron 文件等），应用身份无法读写，可能在日志初始化前退出。

排障时使用应用自身身份，不要用其他账号运行 Hermes。

### 6.2 日志位置

- `@appdata/hermes-desktop/info.log`：生命周期和安装日志；
- `@appshare/hermes-desktop/logs/gateway.log`：Hermes 主日志；
- `@appdata/hermes-desktop/gateway-adapter.log`：adapter 日志；
- `logs/gateway-exit-diag.log`：异常退出诊断。

### 6.3 配置符号链接

配置实体在 `@appshare`，`@appconf` 和 `@appdata/config.yaml` 都是符号链接。直接使用
`sed -i` 会把符号链接替换成普通文件，造成配置分裂。需要编辑时使用实体文件，或
`sed -i --follow-symlinks`。

### 6.4 WebSocket

WebSocket 不能使用普通 HTTP 客户端转发；握手后必须做裸 socket 双向帧转发。
否则会出现 `Desktop IPC bridge is unavailable`。

### 6.5 插件加载

浏览器版只加载构建时打入主 bundle 的插件；Electron 专属的磁盘插件目录不会被浏览器加载。
随包插件需要在构建阶段注入到前端 bundle。

## 7. 构建与安装

构建流程：

1. 获取上游 Release tag；
2. 构建 Webapp 渲染层；
3. 注入 `packaging/fpk/app/webapp/`；
4. 写入 manifest 版本和 commit；
5. 运行布局/契约检查；
6. 生成 fpk 和 build-info。

安装流程：

1. 安装 fnOS Python 依赖；
2. 创建 Python 3.14 venv；
3. 从上游源码安装 Hermes；
4. 写入配置和访问信息；
5. 启动 Hermes gateway 和 adapter；
6. 创建应用文件目录和符号链接。

模型配置在 venv 创建前由 `cmd/apply_model_config.py` 写入；API Key 只保存在本机，文件权限 0600。

## 8. 已知限制

- 浏览器版没有 Electron 的托盘、原生通知、全局快捷键、原生文件选择器、麦克风等能力；
- 登录后等同于获得本机命令执行权限，不能直接暴露公网；
- 飞牛应用中心 CLI 不支持升级/卸载，需要使用 Web UI；
- 旧登录 Cookie 可能导致内嵌登录回环，清除站点 Cookie 即可；
- 卸载不完整可能留下 `venv`，需要手动删除后重装。

## 9. 相关文档

- [当前状态](./CURRENT-STATE.md)
- [包结构](./APP-STRUCTURE.md)
- [内嵌链路](./EMBEDDING.md)
- [冻结契约](./REFACTOR-CONTRACT.md)
