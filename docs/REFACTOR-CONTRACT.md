# REFACTOR CONTRACT（冻结）

> 本文件是本次重构的唯一决策来源。所有成员开工前先读；改动需 Lead 更新本文件。
> 目标仓库镜像：`hermes-src/`（对应 sfy2022/fnos-hermes-desktop@main）。
> 真机：`ssh e5`（192.168.1.2，用户 sun，fnOS/Debian 12，x86_64）。

## 1. 目标

1. 修复桌面内嵌：`iframe → fnOS 网关 → target/app.sock → adapter → 127.0.0.1:9120`。
2. 按飞牛官方规范重整 FPK 包结构与运行期 `etc/var/home` 布局。
3. 删除现有 `gateway_proxy.py` + `fnos_bridge.py` 的 HTML/JS 改写方案。

## 2. 根因与核心决策（已验证）

- 上游 Webapp 返回 CSP `frame-ancestors 'none'`（commit `8d91e48e` 的
  `hermes_cli/webapp_page_policy.py`），浏览器拒绝 iframe。
- 同一 CSP 使用内联脚本 sha256 白名单、`base-uri 'none'`、`form-action 'none'`；
  **禁止任何 HTML/JS/CSS 响应体改写**，否则哈希失效、白屏。
- 上游原生支持 `X-Forwarded-Prefix`：资源 URL、`__HERMES_BASE_PATH__`、登录跳转、
  Cookie Path 均由上游处理；adapter 不再重写 Location/Set-Cookie。
- adapter 只允许改一个响应头：`Content-Security-Policy` 中的
  `frame-ancestors 'none'` → `frame-ancestors 'self'`；`X-Frame-Options: DENY` → `SAMEORIGIN`。
  直连 `:9120` 不经过 adapter，CSP 保持 `'none'`。

## 3. 目标目录

### 3.1 包结构（`packaging/fpk/`）

```
manifest
ICON.PNG (64x64)  ICON_256.PNG (256x256)
app/
  ui/{config, images/{icon_64.png, icon_256.png[, icon_512.png]}}
  webapp/                      # 构建注入，不提交
  runtime/
    gateway/{gateway_adapter.py, gateway.conf}
    lib/libs.sh                # 从 cmd/libs.sh 移入
  plugins/  skills/
cmd/                           # 官方 9 脚本 + 内部辅助
  main, install_init, install_callback, upgrade_init, upgrade_callback,
  uninstall_init, uninstall_callback, config_init, config_callback
  pause_gateway.py             # 保留：卸载回调期 target 可能已删除
config/{privilege, resource}
wizard/{install, config, upgrade, uninstall}
```

- 包根只允许以上条目；不得有 `README`、`docs`、`tests`、`__pycache__`、`*.pyc`、`node_modules`、`dist`、`*.bak`。
- `cmd/` 必须含 9 个生命周期脚本；`pause_gateway.py` 为允许的内部辅助。
- `app/runtime/gateway/gateway.conf`：
  ```
  prefix=/app/hermes-desktop
  socket_name=app.sock
  ```
  是 prefix/socket 的单一来源；`app/ui/config`、`cmd/main`、adapter 三方必须一致。
- `app/ui/images/icon_512.png` 允许保留（高 DPI），但 64/256 必须存在且被入口引用。

### 3.2 安装后布局

```
/var/apps/hermes-desktop/
  cmd/ config/ wizard/ manifest ICON*
  target/            # 只读：ui/ webapp/ runtime/ plugins/ skills/
  etc/               # 文件 0600、目录 0700：config.yaml、access-info.txt、gateway.conf
  var/               # HERMES_HOME：venv/ src/ logs/ sessions/ state/ cache/
                     #              gateway.sock app.pid gateway-adapter.pid gateway-adapter.log
                     #              config.yaml -> ../etc/config.yaml
  home/              # HOME=TRIM_PKGHOME（.config/trim-cli 等）
  tmp/
  shares/            # resource 不声明 data-share 时为空
```

- 配置只写 `TRIM_PKGETC`；运行数据只写 `TRIM_PKGVAR`；HOME 只用 `TRIM_PKGHOME`；`target/` 只读。
- 禁止硬编码 `/vol1`、`/var/apps`。

## 4. manifest / config 冻结值

- `platform="x86"`（包内含 x86_64/arm64 trim-cli 二进制；当前只发 x86_64）。
- 移除 `micro_app`；保留 `appname/version/display_name/desc/source=thirdparty/maintainer/
  maintainer_url/distributor/distributor_url/desktop_uidir=ui/desktop_applaunchname/
  service_port=9120/checkport=false/ctl_stop=true/install_dep_apps=python312`。
- `os_min_version` 不写（待真机实测）。
- `config/privilege`：`run-as=package`，username/groupname=hermes-desktop；无 join-groups/root。
- `config/resource`：`{}`（确认无共享目录使用；如代码真用到才保留最小项）。
- `wizard/*`：JSON 数组；字段 `wizard_*`；禁止 `TRIM_` 自定义字段；不写明文密钥。

## 5. adapter 契约（`app/runtime/gateway/gateway_adapter.py`）

- 纯标准库，可被 `python -m unittest` 导入测试。
- CLI：
  ```
  python3 gateway_adapter.py --socket <path> --upstream 127.0.0.1:<port> \
      --prefix /app/hermes-desktop [--health-path /.gateway-health] [--log-file <path>]
  ```
- 入站路径剥 prefix（严格边界）；注入/覆盖 `X-Forwarded-Prefix`；`Host` 指向上游；
  `Origin/Referer` 重写为上游地址；透传 `Cookie`、`X-Trim-*`、`Authorization`、`Sec-WebSocket-*`；
  剥 hop-by-hop 头。
- 响应：**body 零改写**；只改上述安全头；流式转发（SSE/大文件）；HEAD/204/304 无 body。
- chunked 请求体要解码；头/体上限；超时；健康端点；SIGTERM 清理 socket。
- WS：单一路径；握手头转发 + Origin/Referer 重写；101 后帧双向透传。
- 禁止：改写 HTML/CSS/JS、注入脚本、改 `<base>`、改 Location、改 Set-Cookie、缓冲整个响应。

## 6. 路径与命令冻结

- 构建：`bash scripts/build.sh`（e5 上 PATH 加 Node 26，`APP_VERSION=0.22.13`）。
- 布局检查：`python3 scripts/check_fpk_layout.py packaging/fpk`（模板模式允许缺 `app/webapp`）。
- 契约检查：`python3 scripts/check_gateway_contract.py packaging/fpk`。
- 单测：`python3 -m unittest discover -s tests -v`。
- 安装：`appcenter-cli install-fpk dist/*.fpk`（e5，user sun；若需 root 则停下报告）。
- 验收：`curl --unix-socket /var/apps/hermes-desktop/target/app.sock http://localhost/app/hermes-desktop/`；
  `curl -sSI http://127.0.0.1:9120/`；网关 `:5666/app/hermes-desktop`；WS 101。

## 7. 写作用域

| 成员 | 写作用域 |
|---|---|
| lead | `docs/REFACTOR-CONTRACT.md`、`app/runtime/gateway/gateway_adapter.py`、集成修复 |
| fpk-architect | `docs/APP-STRUCTURE.md`、`docs/EMBEDDING.md` |
| fpk-builder | `packaging/fpk/**`（除 gateway_adapter.py）、`scripts/build.sh` |
| fpk-qa | `scripts/check_fpk_layout.py`、`scripts/check_gateway_contract.py`、`tests/**`、`.github/workflows/test.yml` |
| fpk-frontend | `packaging/fpk/app/ui/config`、`tests/browser/**` |

## 8. 不允许

- 修改 main 分支、发 Release。
- 使用 `sudo` 密码（无密码可用）；安装失败则停止并报告。
- 把上游代码整篇复制进仓库；文档摘要 + 链接。
- 把 `TRIM_API_TOKEN`、Cookie、密码写入日志/报告。
