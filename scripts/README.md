# scripts/ — 构建与运维工具

本目录只放**开发与运维工具**，不放运行时代码。

| 位置 | 内容 | 说明 |
|---|---|---|
| `scripts/` | 构建、冒烟、发布、归档、静态检查 | 不进 fpk 包 |
| `packaging/fpk/cmd/` | 9 个 fnOS 生命周期脚本 + `pause_gateway.py` | 随包分发 |
| `packaging/fpk/app/runtime/` | 网关适配层、运行期辅助库 | 随包分发（`target/runtime/`） |

## 运行时脚本

| 文件 | 作用 |
|---|---|
| `cmd/main` | 启停/状态/diagnose（fnOS 生命周期入口） |
| `cmd/install_callback` | 安装后：写 etc 配置、装依赖、部署插件/Skill |
| `cmd/upgrade_callback` | 升级后：迁移配置、清理旧适配器、重装依赖 |
| `cmd/uninstall_init` / `uninstall_callback` | 卸载：停进程、归档、按选择清理 |
| `cmd/config_*` | 配置变更钩子 |
| `cmd/pause_gateway.py` | 通过 gateway 控制 socket 优雅停机（内部辅助） |
| `app/runtime/gateway/gateway_adapter.py` | fnOS 统一网关薄适配层（HTTP/WS over Unix Socket） |
| `app/runtime/gateway/gateway.conf` | `prefix` / `socket_name` 单一来源 |
| `app/runtime/lib/libs.sh` | 运行期库辅助（libatomic 预置等） |

> 运行时脚本只放在 `packaging/fpk/`，构建时直接取用并做语法/结构校验，不再维护 `scripts/` 副本。

## 访问方式

桌面提供两个入口，共用同一个后端：

1. **直连**：`type: url` + `port: 9120`，浏览器打开 `http://<NAS_IP>:9120/`；
   不经过 fnOS 网关，CSP 保持 `frame-ancestors 'none'`。
2. **桌面内嵌**：`type: iframe` + `gatewayPrefix=/app/hermes-desktop` +
   `gatewaySocket=app.sock`；经 fnOS 统一网关复用飞牛登录态，adapter 仅把 CSP 的
   `frame-ancestors` 放宽为 `'self'`，其余 CSP/响应体不改写。

内嵌细节与排障见 `docs/EMBEDDING.md`，目录规范见 `docs/APP-STRUCTURE.md`。

## 构建与校验

```bash
bash scripts/smoke.sh              # 快速冒烟：结构检查 + 占位包打包
python3 scripts/check_fpk_layout.py packaging/fpk --template
python3 scripts/check_gateway_contract.py packaging/fpk
bash scripts/build.sh              # 完整构建（Node >= 26）
```

冒烟检查项：

1. `packaging/fpk/` 模板文件齐全（含 runtime adapter/gateway.conf/libs.sh）
2. 所有 shell / Python 运行时脚本语法正确
3. 运行时脚本 LF 换行、JSON 合法
4. 组装占位包后跑 `check_fpk_layout.py`
5. 用占位渲染层打出 fpk
6. 产物包含 manifest / cmd / wizard / app.tgz

改动打包脚本后先跑这个，再跑完整构建。
