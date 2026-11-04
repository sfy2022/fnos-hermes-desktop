# 应用包与安装后目录规范（APP-STRUCTURE）

> 适用目录：`packaging/fpk/`（源模板）与 `/var/apps/hermes-desktop/`（安装后布局）。
> 当前版本与发布信息见 [CURRENT-STATE.md](./CURRENT-STATE.md)。
> 内嵌链路见 [EMBEDDING.md](./EMBEDDING.md)，冻结契约见 [REFACTOR-CONTRACT.md](./REFACTOR-CONTRACT.md)。

---

## 1. 一页速览

| # | 规则 | 落地 |
|---|---|---|
| 1 | 配置实体在应用文件，运行数据在 `var/`，HOME 在 `home/` | 路径由 `TRIM_APPDEST` / `TRIM_PKGETC` / `TRIM_PKGVAR` / `TRIM_PKGHOME` 推导，不硬编码卷路径 |
| 2 | `target/` 只读 | 运行期不得写入 `TRIM_APPDEST` |
| 3 | `gateway.conf` 是 prefix/socket 的单一来源 | `app/ui/config`、`cmd/main`、adapter 三方一致 |
| 4 | 包根只允许白名单条目 | 不允许把 `README`、`docs`、`tests`、缓存或构建产物打进 fpk |

桌面只声明一个内嵌入口 `Hermes`；直连 `http://<NAS_IP>:9120` 不创建第二个入口，两者访问同一后端和同一份会话。

---

## 2. 包结构（`packaging/fpk/`）

```text
packaging/fpk/
├── manifest                       # 应用元数据
├── ICON.PNG                       # 64×64
├── ICON_256.PNG                   # 256×256
├── app/
│   ├── ui/
│   │   ├── config                 # 桌面入口
│   │   └── images/
│   │       ├── icon_64.png
│   │       ├── icon_256.png
│   │       └── icon_512.png
│   ├── webapp/                    # 构建时注入，不提交
│   ├── runtime/
│   │   ├── gateway/
│   │   │   ├── gateway_adapter.py
│   │   │   └── gateway.conf
│   │   └── lib/
│   │       └── libs.sh
│   ├── plugins/                   # 随包插件（app-updater、fnos-auth）
│   └── skills/                    # 内置 fnOS Skill（fnos-trim）
├── cmd/                           # 生命周期脚本
│   ├── main
│   ├── install_init
│   ├── install_callback
│   ├── upgrade_init
│   ├── upgrade_callback
│   ├── uninstall_init
│   ├── uninstall_callback
│   ├── config_init
│   ├── config_callback
│   └── pause_gateway.py
├── config/
│   ├── privilege
│   └── resource
└── wizard/
    ├── install
    ├── config
    ├── upgrade
    └── uninstall
```

### 2.1 包根白名单

| 条目 | 允许 | 说明 |
|---|---|---|
| `manifest` | ✅ | 官方要求位于包根 |
| `ICON.PNG`、`ICON_256.PNG` | ✅ | 官方打包检查要求 |
| `app/` | ✅ | 运行文件根目录 |
| `cmd/` | ✅ | 9 个生命周期脚本 + `pause_gateway.py` |
| `config/` | ✅ | `privilege`、`resource` |
| `wizard/` | ✅ | 4 个向导 JSON |
| `README*`、`docs/` | ❌ | 留在仓库 |
| `tests/`、`scripts/`、`.github/` | ❌ | 仓库级资产 |
| `__pycache__/`、`*.pyc` | ❌ | 构建产物 |
| `node_modules/`、`dist/`、`.build/` | ❌ | 构建产物 |
| `*.bak`、`*.log`、`*.pid`、`*.sock`、`venv/` | ❌ | 编辑/运行残留 |

`app/webapp/` 是唯一允许“构建时注入、不提交”的目录。`scripts/build.sh` 每次从上游
Webapp 产物复制，`.gitignore` 已忽略它。

---

## 3. 关键文件

### 3.1 `manifest`

| 字段 | 值/规则 | 说明 |
|---|---|---|
| `appname` | `hermes-desktop` | 决定安装目录、入口前缀和网关前缀 |
| `version` | 构建时注入 | 与上游 Release tag 一致；源文件中的版本只是模板占位 |
| `platform` | `x86` | 当前发布 x86_64 |
| `source` | `thirdparty` | 第三方分发 |
| `desktop_uidir` | `ui` | 入口配置在 `app/ui/config` |
| `desktop_applaunchname` | `hermes-desktop.Embedded` | 必须等于入口 ID |
| `service_port` | `9120` | 上游 webapp 端口 |
| `checkport` | `false` | 应用自行处理端口冲突 |
| `ctl_stop` | `true` | 需要启停 |
| `install_dep_apps` | `python312` | 依赖 fnOS Python 运行时 |
| `micro_app` | 不写 | — |
| `os_min_version` | 不写 | 当前不限制 |

`build.sh` 会把实际版本写入 `manifest`，并在 `desc` 中补充上游 commit 和入口说明。

### 3.2 `config/privilege`

```json
{
  "defaults": { "run-as": "package" },
  "username": "hermes-desktop",
  "groupname": "hermes-desktop"
}
```

不使用 root，也不加入额外用户组。

### 3.3 `config/resource`

```json
{
  "data-share": {
    "shares": [
      { "name": "hermes-desktop" }
    ]
  }
}
```

`data-share` 让飞牛文件管理的“应用文件”显示 `hermes-desktop` 目录。
安装和启动时创建 `/vol{n}/@appshare/hermes-desktop`，其中包含：

```text
config/       # config.yaml、access-info.txt、gateway.conf
logs/
workspace/
data -> @appdata/hermes-desktop
README.txt
```

### 3.4 `wizard/`

四个向导文件都是 JSON 数组，字段使用 `wizard_*` 前缀，禁止自定义 `TRIM_` 字段。

安装向导当前三步：

1. 访问账号：`wizard_web_user`、`wizard_web_pass`
2. 模型配置：`wizard_model_provider`、`wizard_model_api_key`、`wizard_model_name`、
   `wizard_model_base_url`
3. 网络配置：`wizard_github_proxy`、`wizard_pypi_index`

模型配置由 `cmd/apply_model_config.py` 在创建 venv 前用系统 Python 写入：

- `config.yaml` 的 `model` 段；
- `.env` 中的 provider key；
- 文件权限 0600，key 不进入日志。

### 3.5 `app/ui/config`

```json
{
  ".url": {
    "hermes-desktop.Embedded": {
      "title": "Hermes",
      "icon": "images/icon_{0}.png",
      "type": "iframe",
      "gatewayPrefix": "/app/hermes-desktop",
      "gatewaySocket": "app.sock",
      "url": "/app/hermes-desktop/",
      "allUsers": true,
      "control": { "accessPerm": "readonly" }
    }
  }
}
```

- `desktop_applaunchname` 指向 `hermes-desktop.Embedded`。
- `gatewayPrefix` / `gatewaySocket` 必须与 `gateway.conf` 一致。
- 不写 `protocol` 或 `port`；直连端口只作为文档提示。
- 入口 URL 必须带尾斜杠，否则相对资源会解析到错误路径。

### 3.6 `app/runtime/gateway/gateway.conf`

```ini
prefix=/app/hermes-desktop
socket_name=app.sock
forwarded_proto=http
```

- `prefix`、`socket_name` 是单一来源，`cmd/main`、adapter、`app/ui/config` 三方一致。
- `socket_name` 只写文件名，实际路径为 `${TRIM_APPDEST}/app.sock`。
- `forwarded_proto=http` 时 adapter 覆盖上游 `X-Forwarded-Proto`，并去掉认证 Cookie 的
  `Secure`/`__Secure-` 属性；HTTPS 网关可改为 `https`。

### 3.7 图标

- 根目录必须有 `ICON.PNG`、`ICON_256.PNG`。
- `app/ui/images/icon_64.png`、`icon_256.png` 必须存在并被入口引用。
- `icon_512.png` 可选，用于高 DPI。

---

## 4. 安装后布局

```text
/var/apps/hermes-desktop/
├── manifest  ICON.PNG  ICON_256.PNG  cmd/  config/  wizard/
├── target -> /vol{n}/@appcenter/hermes-desktop
│   ├── ui/
│   ├── webapp/
│   ├── runtime/gateway/{gateway_adapter.py, gateway.conf}
│   ├── runtime/lib/libs.sh
│   ├── plugins/  skills/
│   └── app.sock                    # adapter 监听的 Unix Socket
├── etc -> /vol{n}/@appconf/hermes-desktop
│   ├── config.yaml -> @appshare/.../config/config.yaml
│   ├── access-info.txt -> @appshare/.../config/access-info.txt
│   └── gateway.conf -> @appshare/.../config/gateway.conf
├── var -> /vol{n}/@appdata/hermes-desktop
│   ├── config.yaml -> /vol{n}/@appconf/hermes-desktop/config.yaml
│   ├── logs -> /vol{n}/@appshare/hermes-desktop/logs
│   ├── venv/  src/  sessions/  state/  cache/
│   ├── gateway.sock
│   ├── app.pid
│   └── gateway-adapter.pid / gateway-adapter.log
├── home -> /vol{n}/@apphome/hermes-desktop
├── tmp -> /vol{n}/@apptemp/hermes-desktop
├── meta -> /vol{n}/@appmeta/hermes-desktop
└── shares/
```

### 4.1 配置实体

```text
/vol1/@appshare/hermes-desktop/config/config.yaml
/vol1/@appshare/hermes-desktop/config/access-info.txt
/vol1/@appshare/hermes-desktop/config/gateway.conf
```

- 文件权限 0600，目录 770。
- `@appconf` 中的同名文件全部是绝对目标符号链接。
- `@appdata/config.yaml` 指向 `@appconf`，保证上游按 `HERMES_HOME` 能找到配置。
- 不要用 `sed -i` 直接编辑符号链接；需要修改时编辑 `@appshare` 实体文件，或使用
  `sed -i --follow-symlinks`。

### 4.2 两个 socket

| Socket | 位置 | 监听者 | 用途 |
|---|---|---|---|
| `app.sock` | `target/app.sock` | `gateway_adapter.py` | 飞牛统一网关入口 |
| `gateway.sock` | `var/gateway.sock` | Hermes gateway | 应用内控制面（停机等） |

两者不可混用。adapter 启动前会删除残留的 `app.sock`；退出时也应清理。

### 4.3 运行数据

`var/` 由应用用户可写，包含但不限于：

- `venv/`、`src/`：Python 环境和上游源码；
- `sessions/`、`state/`、`cache/`、`memories/`、`cron/`；
- `bin/`、`pythons/`、`.uv-cache/`、`lib/`；
- `skills/`、`plugins/`、`desktop-plugins/`；
- `logs/`（链接到 `@appshare`）。

`target/` 只读；运行期不得把日志、PID、socket 写到 `target/`。

---

## 5. 数据写入纪律

| 内容 | 位置 | 权限 | 禁止 |
|---|---|---|---|
| 用户配置（含密码哈希） | `@appshare/.../config/config.yaml` | 0600 | 写入 `var/` 或日志 |
| 访问信息 | `@appshare/.../config/access-info.txt` | 0600 | 打印到日志 |
| 网关配置 | `target/runtime/gateway/gateway.conf` + `@appshare/.../config/gateway.conf` | 0600 | 三方各写常量 |
| 运行数据 | `@appdata/hermes-desktop` | 应用用户 | 写入 `target/` |
| HOME 相关（trim-cli 凭据） | `@apphome/hermes-desktop` | 应用用户 | 写入 `var/` |
| 日志 | `@appshare/.../logs`、`@appdata/info.log` | 应用用户 | 含 Cookie / Token / 明文密码 |

---

## 6. 卸载与升级边界

- 卸载选择“清除数据”时，归档配置、会话、记忆、技能授权，并删除 `@appshare` 配置；
- `venv/`、`src/` 不归档，安装时可重建；
- 升级时保留用户配置和会话；
- `pause_gateway.py` 保留在 `cmd/`，因为卸载阶段 `target/` 可能已经被删除，不能依赖
  `target/` 下的脚本。

---

## 7. 构建与检查

构建使用 `scripts/build.sh`，需要 Node.js >= 26 和 fnpack：

```bash
UPSTREAM_REF=v0.22.7 APP_VERSION=0.22.7 bash scripts/build.sh
```

`build.sh` 会：

1. 获取指定上游 tag；
2. 构建 Webapp 渲染层；
3. 复制到 `app/webapp/`；
4. 写入 manifest 版本和描述；
5. 运行布局与网关契约检查；
6. 生成 `dist/Hermes-Desktop-fnOS-v<版本>-<commit>.fpk` 和 `dist/build-info.json`。

布局检查以 `scripts/check_fpk_layout.py` 为准；网关契约以
`scripts/check_gateway_contract.py` 为准。源码树中缺少 `app/webapp/` 属于正常现象，
构建时注入。

---

## 8. 相关文档

- [当前状态](./CURRENT-STATE.md)
- [内嵌链路](./EMBEDDING.md)
- [冻结契约](./REFACTOR-CONTRACT.md)
- [历史设计记录](./IMPLEMENTATION.md)
