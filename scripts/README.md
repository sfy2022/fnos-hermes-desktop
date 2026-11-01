# scripts/ — 构建与运维工具

本目录只放**开发与运维工具**，不放运行时代码。

| 位置 | 内容 | 说明 |
|---|---|---|
| `scripts/` | 构建、冒烟测试、发布、归档、静态检查 | 不进 fpk 包 |
| `packaging/fpk/cmd/` | 应用运行时脚本 | **唯一来源**，随包分发 |

运行时脚本（`packaging/fpk/cmd/`）：

| 文件 | 作用 |
|---|---|
| `main` | 应用启停与状态检查（fnOS 生命周期入口） |
| `install_callback` | 安装后：写配置、装依赖、部署网关脚本 |
| `uninstall_init` / `uninstall_callback` | 卸载：停进程、归档数据、按选择清理 |
| `upgrade_callback` | 升级后处理 |
| `gateway_proxy.py` | fnOS 统一网关适配层（HTTP over Unix Socket） |
| `fnos_bridge.py` | 注入页面的前缀桥接脚本（把绝对路径 API 改写成带前缀） |
| `pause_gateway.py` | 通过 gateway 的 Unix Socket 做优雅停机 |

> 早期版本曾在 `scripts/` 下放这些运行时脚本的副本，靠打包前比对保证一致。
> 踩过一次「改了源码没同步副本 → 包里还是旧版」的坑，现已改为单一来源：
> 运行时脚本只放在 `packaging/fpk/cmd/`，构建时直接取用并做语法校验。

# 冒烟自检

不构建完整渲染层（约 1 分钟），验证打包链路是否正常。

```bash
bash scripts/smoke.sh
```

检查项：

1. `packaging/fpk/` 模板文件齐全
2. 所有 shell 脚本语法正确（`bash -n`）
3. 所有 JSON 文件合法（`app/ui/config`、`config/*`、`wizard/*`）
4. 用占位渲染层能成功打出 fpk
5. 产物包含 manifest / cmd / wizard 等必需条目

改动打包脚本后先跑这个，再跑完整构建。
