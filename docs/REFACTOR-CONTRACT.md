# 内嵌与打包契约（REFACTOR-CONTRACT）

> 本文件是内嵌链路、包结构和运行期布局的冻结契约。
> 当前版本、发布流程和运行状态见 [CURRENT-STATE.md](./CURRENT-STATE.md)。

## 1. 目标

1. 桌面内嵌链路：`iframe → fnOS 网关 → target/app.sock → adapter → 127.0.0.1:9120`。
2. 按 fnOS 规范组织 FPK 包结构和运行期布局。
3. 禁止旧的 HTML/JS 改写方案。

## 2. 根因与核心决策

- 上游 Webapp 下发 CSP `frame-ancestors 'none'`，浏览器拒绝 iframe。
- 同一 CSP 使用内联脚本 sha256 白名单、`base-uri 'none'`、`form-action 'none'`；
  因此禁止任何 HTML/CSS/JS 响应体改写。
- 上游原生支持 `X-Forwarded-Prefix`，资源 URL、路由基路径、登录跳转和 Cookie Path
  由上游处理；adapter 不修改 Location，也不修改 Cookie value/Path。
- fnOS HTTP 网关下设置 `forwarded_proto=http`，adapter 覆盖上游 `X-Forwarded-Proto`，
  并去掉认证 Cookie 的 `Secure`/`__Secure-` 属性；这是唯一的 Cookie 例外。
- adapter 只调整：
  - CSP `frame-ancestors 'none'` → `'self'`；
  - `X-Frame-Options: DENY` → `SAMEORIGIN`；
  - HTTP 模式下认证 Cookie 的 Secure/`__Secure-`。
- 直连 `:9120` 不经过 adapter，CSP 保持 `'none'` / `DENY`。

## 3. 包结构

```text
manifest
ICON.PNG (64×64)  ICON_256.PNG (256×256)
app/
  ui/{config, images/{icon_64.png, icon_256.png[, icon_512.png]}}
  webapp/                      # 构建注入，不提交
  runtime/
    gateway/{gateway_adapter.py, gateway.conf}
    lib/libs.sh
  plugins/  skills/
cmd/                           # 官方 9 脚本 + pause_gateway.py
config/{privilege, resource}
wizard/{install, config, upgrade, uninstall}
```

- 包根只允许以上条目；不得包含 `README`、`docs`、`tests`、缓存、构建产物或运行残留。
- `cmd/` 必须包含 9 个生命周期脚本；`pause_gateway.py` 是允许的内部辅助。
- `app/ui/images/icon_512.png` 可选，64/256 必须存在并被入口引用。

### 3.1 `gateway.conf`

```ini
prefix=/app/hermes-desktop
socket_name=app.sock
forwarded_proto=http
```

- `prefix` / `socket_name` 是单一来源，`app/ui/config`、`cmd/main`、adapter 三方必须一致。
- `forwarded_proto` 可选 `http` / `https`；默认 `http` 适配 fnOS 5666。
- 运行期入口：`target/app.sock`。

## 4. 安装后布局

```text
/var/apps/hermes-desktop/
  cmd/ config/ wizard/ manifest ICON*
  target -> /vol{n}/@appcenter/hermes-desktop
  etc    -> /vol{n}/@appconf/hermes-desktop
  var    -> /vol{n}/@appdata/hermes-desktop
  home   -> /vol{n}/@apphome/hermes-desktop
  tmp    -> /vol{n}/@apptemp/hermes-desktop
  meta   -> /vol{n}/@appmeta/hermes-desktop
  shares/
```

配置实体：

```text
/vol{n}/@appshare/hermes-desktop/config/{config.yaml,access-info.txt,gateway.conf}
```

- `@appconf` 中的三个配置文件是指向 `@appshare` 的绝对目标符号链接；
- `@appdata/config.yaml` 指向 `@appconf`；
- `@appdata/logs` 指向 `@appshare/.../logs`；
- 配置只写实体文件；运行数据只写 `@appdata`；HOME 只写 `@apphome`；`target/` 只读。

## 5. manifest / config 冻结值

- `platform="x86"`（只发布 x86_64）。
- 移除 `micro_app`；保留 `appname/version/display_name/desc/source=thirdparty/
  maintainer/maintainer_url/distributor/distributor_url/desktop_uidir=ui/
  desktop_applaunchname/service_port=9120/checkport=false/ctl_stop=true/
  install_dep_apps=python312`。
- `version` 由构建注入，值等于上游 Release tag；不本地递增。
- `os_min_version` 暂不写。
- `config/privilege`：`run-as=package`，username/groupname=`hermes-desktop`，无 root、无额外组。
- `config/resource`：只声明 `data-share`，共享名 `hermes-desktop`。
- `wizard/*`：JSON 数组，字段使用 `wizard_*`，禁止自定义 `TRIM_` 字段；密码字段不进入日志。

## 6. adapter 契约

```bash
python3 gateway_adapter.py \
  --socket <path> \
  --upstream 127.0.0.1:<port> \
  --prefix /app/hermes-desktop \
  --forwarded-proto http \
  [--health-path /.gateway-health] \
  [--log-file <path>]
```

- 只使用标准库。
- 严格剥 prefix；注入/覆盖 `X-Forwarded-Prefix`；`Host` 指向上游；重写 `Origin`/`Referer`；
  透传 Cookie、`X-Trim-*`、Authorization、WebSocket 头；剥 hop-by-hop 头。
- 响应 body 零改写；只调整 CSP frame 指令、XFO 和 HTTP 认证 Cookie 属性。
- 支持 chunked 请求、SSE、大文件、HEAD/204/304。
- WebSocket 握手后做双向裸 socket 转发。
- 健康端点用于本机排障。
- 启动/退出清理 socket，重复启动幂等。

禁止：

```text
× 改写 HTML/CSS/JS、注入脚本、修改 <base>
× 修改 Location、Cookie value/Path
× 修改认证 Cookie 的 Secure 之外属性
× 缓冲整个响应
× 新增 TCP 监听
× 记录 Cookie、Authorization、Token、密码
```

## 7. 构建与安装

- 构建：`UPSTREAM_REF=<tag> APP_VERSION=<version> bash scripts/build.sh`。
- 安装：通过飞牛应用中心 Web UI 手动安装；应用中心 CLI 不支持升级/卸载。
- 升级保留配置和会话；卸载“清除数据”时归档配置、会话、授权并删除 `@appshare` 配置。

## 8. 相关文档

- [当前状态](./CURRENT-STATE.md)
- [包结构](./APP-STRUCTURE.md)
- [内嵌链路](./EMBEDDING.md)
- [历史设计记录](./IMPLEMENTATION.md)
