# fnOS 授权插件 — 安装说明

Hermes 原生插件（工具 + dashboard tab），用于给多台 fnOS 设备做 OAuth 授权。

## 放进仓库

解到 Hermes 源码树的 `plugins/` 下：

```bash
tar xzf fnos-auth-1.0.0.tar.gz -C <hermes-src>/plugins/
# 结果：<hermes-src>/plugins/fnos-auth/{plugin.yaml,__init__.py,dashboard/}
```

插件目录必须叫 `fnos-auth`，且 `dashboard/manifest.json` 里的 `name` 与之一致。

## 依赖

二维码需要 `segno`（可选，缺了只是不显示二维码）：

```bash
uv pip install --python <DATA_DIR>/venv/bin/python segno
```

`trim-cli` 来自技能包 `skills/fnos-trim/scripts/trim-cli`（这个应用已经自带）。

## 启用

界面 tab 和 `/api/plugins/fnos-auth/` 在 bundled 状态下自动挂载，**工具需要显式启用**：

```bash
hermes plugins enable fnos-auth
# 或 config.yaml:
# plugins:
#   enabled:
#     - fnos-auth
```

改完重启应用（gateway 缓存了插件表），之后左侧导航出现「fnOS 授权」，
新会话里出现 `fnos_auth_*` 工具。

## 校验

```bash
hermes plugins validate plugins/fnos-auth
hermes plugins list | grep fnos-auth
```

## 结构

```
fnos-auth/
├── plugin.yaml              # provides_tools: fnos_auth_*
├── __init__.py              # register() → 6 个工具
└── dashboard/
    ├── manifest.json        # tab /fnos-auth, entry/css/api
    ├── dist/index.js        # window.__HERMES_PLUGINS__.register("fnos-auth", Page)
    ├── dist/style.css
    └── plugin_api.py        # FastAPI router + 核心实现（pty 会话/设备清单/二维码）
```

工具：`fnos_auth_devices`、`fnos_auth_add`、`fnos_auth_start`、
`fnos_auth_submit`、`fnos_auth_check`、`fnos_auth_logout`。

## 注意事项

- `trim-cli login` 强制要求 TTY，后端用 `pty.openpty()` 起子进程并关闭回显；不要改成普通管道。
- `trim-cli login` 失败/被中断会清掉该 profile 原有会话，`plugin_api.py` 已实现会话快照与回滚
  （`cache/session-backup/`），不要移除。
- 插件无外部网络依赖，只调用本机 `trim-cli`。
