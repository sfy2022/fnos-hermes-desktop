# 应用包与安装后目录规范（APP-STRUCTURE）

> **决策源**：[REFACTOR-CONTRACT.md](./REFACTOR-CONTRACT.md)（冻结契约）。本文只做落地展开，冲突以契约为准。
> **适用范围**：`packaging/fpk/`（源目录）与安装后的 `/var/apps/hermes-desktop/`（运行期布局）。
> **当前状态基线**：2026-11-03 重构完成后状态（e5 真机安装验收见 §8）。
> 「当前」列只代表该时刻事实；最新状态以 `python3 scripts/check_fpk_layout.py packaging/fpk` 输出为准。
> **官方依据**：包规范主要来自飞牛官方 fnpack/framework 页，字段语义来自 manifest/privilege/resource/wizard 页（见 §3 链接表）。

---

## 1. 一页速览（四条铁律）

| # | 铁律 | 落地 |
|---|---|---|
| 1 | **配置只写 `etc/`，运行数据只写 `var/`，用户 HOME 只用 `home/`** | 全部路径用 `TRIM_PKGETC` / `TRIM_PKGVAR` / `TRIM_PKGHOME` 推导，禁止硬编码 |
| 2 | **`target/` 只读** | 运行期不得写 `TRIM_APPDEST`；随包文件只能读 |
| 3 | **`gateway.conf` 是 prefix/socket 的单一来源** | `app/ui/config`、`cmd/main`、adapter 三方读取一致，不得各写一份常量 |
| 4 | **包根只允许白名单条目** | 见 §2.2，`README/docs/tests/__pycache__/*.pyc/node_modules/dist/*.bak` 一律不得入包 |

外部可见行为：一个应用两个入口（网页直连 `:9120`、飞牛桌面内嵌 `/app/hermes-desktop`），同一后端、同一份会话。内嵌链路细节见 [EMBEDDING.md](./EMBEDDING.md)。

---

## 2. 目标包结构（`packaging/fpk/`）

### 2.1 目录树

```text
packaging/fpk/
├── manifest                       # 应用元数据（见 §4.1 冻结值）
├── ICON.PNG                       # 64×64
├── ICON_256.PNG                   # 256×256
├── app/
│   ├── ui/
│   │   ├── config                 # 桌面入口（桌面图标 / 内嵌入口）
│   │   └── images/
│   │       ├── icon_64.png        # 必须有，被入口 icon_{0} 引用
│   │       ├── icon_256.png       # 必须有
│   │       └── icon_512.png       # 允许保留（高 DPI，非官方硬性要求）
│   ├── webapp/                    # ★ 构建注入，不提交（.gitignore 已忽略 app/webapp/）
│   ├── runtime/
│   │   ├── gateway/
│   │   │   ├── gateway_adapter.py # 薄适配层（纯标准库，契约见 §5）
│   │   │   └── gateway.conf       # prefix/socket 单一来源
│   │   └── lib/
│   │       └── libs.sh            # 从 cmd/libs.sh 迁入
│   ├── plugins/                   # 随包插件（app-updater、fnos-auth）
│   └── skills/                    # 内置飞牛技能（fnos-trim，含双架构 trim-cli）
├── cmd/                           # 官方 9 脚本 + 内部辅助
│   ├── main  install_init  install_callback
│   ├── upgrade_init  upgrade_callback
│   ├── uninstall_init  uninstall_callback
│   ├── config_init  config_callback
│   └── pause_gateway.py           # 允许保留：卸载回调期 target 可能已删除
├── config/
│   ├── privilege                  # run-as=package，username/groupname=hermes-desktop
│   └── resource                   # 目标：{}
└── wizard/
    ├── install  config  upgrade  uninstall   # JSON 数组
```

### 2.2 包根允许 / 禁止清单

| 条目 | 允许 | 说明 |
|---|---|---|
| `manifest` | ✅ | 官方要求在包根、无扩展名 |
| `ICON.PNG`、`ICON_256.PNG` | ✅ | 官方打包检查要求存在（尺寸见 §4.4 团队约定） |
| `app/` | ✅ | 运行文件根目录 |
| `cmd/` | ✅ | 官方 9 个生命周期脚本 + 允许的内部辅助 |
| `config/` | ✅ | `privilege`、`resource` |
| `wizard/` | ✅ | 4 个可选向导文件（目录必须存在） |
| `README*`、`docs/` | ❌ | 文档留在仓库，不进包 |
| `tests/`、`scripts/`、`.github/` | ❌ | 仓库级资产，不进包 |
| `__pycache__/`、`*.pyc` | ❌ | 构建产物 |
| `node_modules/`、`dist/`、`.build/` | ❌ | 构建产物（`dist/` 为输出目录） |
| `*.bak`、`*.orig`、`*.rej` | ❌ | 编辑残留 |
| `*.log`、`*.pid`、`*.sock`、`venv/` | ❌ | 运行期产物，绝不入库/入包 |
| `.gitignore`、`.upstream-sha`、`.upstream-release` | ❌ | 仓库元数据：`.upstream-sha` 追溯构建的上游 commit；`.upstream-release` 记录已构建的上游 Release tag（只有新 Release 才构建） |

> `app/webapp/` 是**唯一**允许「构建时注入、不提交」的目录：`.gitignore` 已忽略它，`scripts/build.sh` 每次从上游产物复制。

### 2.3 各目录职责

| 路径 | 职责 | 只读？ | 备注 |
|---|---|---|---|
| `app/ui/` | 桌面入口配置与图标 | ✅ 只读 | 入口字段见 §4.6 |
| `app/webapp/` | 上游 Webapp 渲染层（`index.html` + 静态资源） | ✅ 只读 | 运行时经 `HERMES_WEBAPP_DIST` 指向 `target/webapp` |
| `app/runtime/gateway/` | 网关薄适配层 + 配置 | ✅ 只读 | 不得出现日志/PID/socket 文件 |
| `app/runtime/lib/` | 运行时库脚本（libatomic 预置） | ✅ 只读 | 纯函数式脚本，被 `cmd/*` source |
| `app/plugins/` | 随包插件（构建时注入 bundle 的源） | ✅ 只读 | `fnos-auth`、`app-updater` |
| `app/skills/` | 内置飞牛技能（含 `trim-cli` 二进制） | ✅ 只读 | 安装时复制到 `var/skills/` |
| `cmd/` | 生命周期脚本 | 可执行 | 官方入口，必须 9 个齐全 |
| `config/` | 权限与资源声明 | ✅ 只读 | 安装前被系统读取 |
| `wizard/` | 安装/配置/升级/卸载表单 | ✅ 只读 | JSON 数组，字段 `wizard_*` |

---

## 3. 官方规范对应关系（摘要 + 链接）

> 只摘与「包/布局」直接相关的硬性点，完整规则以官方页面为准。

| 官方页面 | 官方要点（摘要） | 本包落点 |
|---|---|---|
| [fnpack](https://developer.fnnas.com/docs/cli/fnpack/) | 打包前检查：`manifest`、`config/privilege`、`config/resource`、`ICON.PNG`、`ICON_256.PNG`、`app/`、`cmd/`、`wizard/` 必须存在；声明 `desktop_uidir` 时该目录必须存在；产物必须由 `fnpack build` 生成 | §2.1 全部路径；构建命令见 §6 |
| [应用框架 framework](https://developer.fnnas.com/docs/core-concepts/framework/) | 安装后在 `/var/apps/{appname}` 创建 `target`/`etc`/`var`/`tmp`/`home`/`shares` 等目录；`cmd/` 九个生命周期脚本语义与 `main` 退出码 0/3/1 | §5 运行期布局；`cmd/main` 三态契约 |
| [manifest](https://developer.fnnas.com/docs/core-concepts/manifest/) | `appname/version/display_name/desc/source`；`platform` 枚举；`desktop_uidir` 默认 `ui`；`desktop_applaunchname` 必须与入口 ID 一致；`service_port`/`checkport`/`ctl_stop` 语义 | §4.1 冻结值 |
| [应用权限 privilege](https://developer.fnnas.com/docs/core-concepts/privilege/) | 默认最小权限 `run-as=package`；root 仅限特权准备；用户文件需授权目录或资源声明 | §4.2（已符合） |
| [应用资源 resource](https://developer.fnnas.com/docs/core-concepts/resource/) | 只声明实际使用的能力；`data-share` 使用 Windows ACL 模型，系统自动授权运行用户 | §4.3（只声明 `data-share`） |
| [用户向导 wizard](https://developer.fnnas.com/docs/core-concepts/wizard/) | 文件为 JSON 数组；`field` 即环境变量名；自定义字段不得用 `TRIM_` 前缀；密钥用 `password` 且不得写日志 | §4.5 |
| [环境变量](https://developer.fnnas.com/docs/core-concepts/environment-variables/) | `TRIM_APPDEST/PKGETC/PKGVAR/PKGTMP/PKGHOME` 等路径变量；失败写 `TRIM_TEMP_LOGFILE` | §5 全部路径推导 |

---

## 4. 关键文件冻结值（目标）

### 4.1 `manifest`

| 字段 | 目标值 | 说明 |
|---|---|---|
| `appname` | `hermes-desktop` | 决定安装路径、入口前缀、网关前缀（`/app/hermes-desktop`） |
| `version` | 构建注入（契约示例 `APP_VERSION=0.22.13`） | fnOS 不允许同版本覆盖安装，每次构建必须提版本（当前源文件为 `0.22.6`，由 `build.sh` 覆盖） |
| `platform` | `x86` | 包内含 x86_64/arm64 `trim-cli`，但当前只发 x86_64 |
| `source` | `thirdparty` | — |
| `maintainer` / `maintainer_url` / `distributor` / `distributor_url` | 保留现值 | 第三方分发信息 |
| `desktop_uidir` | `ui` | 入口配置在 `app/ui/config` |
| `desktop_applaunchname` | `hermes-desktop.Embedded` | 必须等于入口 ID |
| `service_port` | `9120` | 上游 webapp 端口；入口 `port` 必须与之一致 |
| `checkport` | `false` | 端口自检关闭（应用自管端口冲突处理） |
| `ctl_stop` | `true` | 需要启停 |
| `install_dep_apps` | `python312` | 依赖官方 Python 运行时 |
| `micro_app` | **移除** | 契约明确移除 |
| `os_min_version` | **不写** | 待真机实测后补（TBD） |
| `desc` | 构建注入 | `build.sh` 会写入「上游 commit …」版本说明，属正常行为 |

### 4.2 `config/privilege`

```json
{
  "defaults": { "run-as": "package" },
  "username": "hermes-desktop",
  "groupname": "hermes-desktop"
}
```

无 `join-groups`、无 root。**当前文件已符合，无需改动**。

### 4.3 `config/resource`

目标：只声明 `data-share`，让飞牛文件管理的「应用文件」出现 `hermes-desktop` 入口。

- 当前声明：
  ```json
  {"data-share": {"shares": [{"name": "hermes-desktop"}]}}
  ```
- 安装/启动时创建 `/vol1/@appshare/hermes-desktop`（770），内含 `README.txt` 及
  `data`、`config`、`logs`、`workspace` 符号链接；真实数据仍在 `@appdata` / `@appconf` / `@apphome`。
- 不再声明其它 resource（网关/Docker 等）。

### 4.4 图标

- 官方硬性：`ICON.PNG`、`ICON_256.PNG` 存在（见 fnpack 页）。
- 团队约定：根目录 64/256 像素；`app/ui/images/icon_64.png`、`icon_256.png` 必须存在且被入口引用；`icon_512.png` 允许保留但非必需。

### 4.5 `wizard/`

- 四个文件均为 **JSON 数组**，字段用 `wizard_*`（如 `wizard_web_user`、`wizard_web_pass`、`wizard_model_provider`、`wizard_model_api_key`、`wizard_model_name`、`wizard_model_base_url`、`wizard_github_proxy`、`wizard_pypi_index`）。
- 禁止自定义 `TRIM_` 前缀字段；`password` 类型字段的值不得写入日志或报告。
- 当前 `install` 三个步骤：访问账号 → 模型配置（OpenRouter/Anthropic/DeepSeek/自定义/跳过）→ 网络配置；
  `config`（说明页）、`upgrade`、`uninstall` 均存在，结构合规。
- 模型配置由 `cmd/apply_model_config.py` 在 venv 创建前用系统 python3 写入：
  `etc/config.yaml` 的 `model` 段 + `var/.env` 的 provider key；内置 provider 的 key 只写 `.env`，
  `custom`/`deepseek` 的 `api_key` 写 config；两文件均 0600，key 不落日志。

### 4.6 `app/ui/config`（单入口：桌面内嵌）

产品决定（2026-11-04）：桌面只保留**一个**内嵌入口，直连不再建图标，端口写在 `manifest.desc` 提示。

| 入口 ID | `type` | 关键字段 | 约束 |
|---|---|---|---|
| `hermes-desktop.Embedded` | `iframe` | `title=Hermes`、`gatewayPrefix=/app/hermes-desktop`、`gatewaySocket=app.sock`、`url=/app/hermes-desktop/` | 唯一桌面入口；`gatewayPrefix`/`gatewaySocket` 必须与 `app/runtime/gateway/gateway.conf` 完全一致；不写 `protocol`/`port` |

`desktop_applaunchname` 指向 `hermes-desktop.Embedded`；`control.accessPerm=readonly` 保持只读。
直连 `http://<NAS_IP>:9120` 仍可用，但不在桌面创建图标，只在应用介绍（`manifest.desc`）与 README 提示。
`scripts/check_fpk_layout.py` 不再要求直连 `type=url` 入口。

### 4.7 `app/runtime/gateway/gateway.conf`

```ini
prefix=/app/hermes-desktop
socket_name=app.sock
```

- 单一来源：`cmd/main`、adapter 启动参数、`app/ui/config` 的网关字段都由它派生（当前 `cmd/main` 里是硬编码常量 `GATEWAY_PREFIX="/app/hermes-desktop"`，迁移后改为读取本文件）。
- `socket_name` 只写文件名；实际路径固定为 `${TRIM_APPDEST}/app.sock`（与 `gatewaySocket` 语义一致，见官方 app-entry 页对 `gatewaySocket` 的要求）。

---

## 5. 安装后运行期布局（目标）

```text
/var/apps/hermes-desktop/
├── cmd/ config/ wizard/ manifest ICON.PNG ICON_256.PNG   # 随包（只读）
├── target/                       # → /vol{n}/@appcenter/hermes-desktop（只读）
│   ├── ui/  webapp/  plugins/  skills/
│   ├── runtime/gateway/{gateway_adapter.py, gateway.conf}
│   ├── runtime/lib/libs.sh
│   └── app.sock                  # ★ 网关 adapter 监听的 Unix Socket（运行期生成）
├── etc/                          # → /vol{n}/@appconf/hermes-desktop（0600）
│   ├── config.yaml               # 上游配置（含 dashboard.basic_auth）
│   ├── access-info.txt           # 访问信息（0600，明文密码按设计落此文件）
│   └── gateway.conf              # 运行期副本（prefix/socket 单一来源）
├── var/                          # → /vol{n}/@appdata/hermes-desktop
│   ├── HERMES_HOME 根
│   ├── config.yaml -> ../etc/config.yaml   # 符号链接：上游按 HERMES_HOME 找配置
│   ├── venv/  src/  logs/  sessions/  state/  cache/
│   ├── gateway.sock              # ★ Hermes 自己的控制 socket（pause_gateway 用）
│   └── app.pid                   # Hermes gateway PID
├── home/                         # → /vol{n}/@apphome/hermes-desktop（HOME=TRIM_PKGHOME）
│   └── .config/trim-cli/         # trim-cli OAuth 凭据
├── tmp/                          # → /vol{n}/@apptemp/hermes-desktop
└── shares/                       # 应用文件共享（对应 @appshare）
```

### 5.1 两个 socket 不要混淆

| Socket | 位置 | 谁监听 | 用途 |
|---|---|---|---|
| `app.sock` | `target/app.sock` | `gateway_adapter.py` | fnOS 统一网关转发入口（`gatewaySocket`） |
| `gateway.sock` | `var/gateway.sock` | Hermes gateway | 应用内控制面（`pause_gateway.py` 停机指令） |

### 5.2 写入纪律

| 内容 | 位置 | 权限 | 禁止 |
|---|---|---|---|
| 用户配置（含密码哈希） | `TRIM_PKGETC/config.yaml` | 0600 | 写进 `var/`、日志、报告 |
| 访问信息 | `TRIM_PKGETC/access-info.txt` | 0600 | 打印到 console/日志 |
| prefix/socket 配置 | 包内 `runtime/gateway/gateway.conf`，运行期副本 `TRIM_PKGETC/gateway.conf` | — | 三方各写常量 |
| 运行数据（会话/记忆/技能） | `TRIM_PKGVAR` | 应用用户 | 写 `target/` |
| HOME 相关（trim-cli 凭据） | `TRIM_PKGHOME` | 应用用户 | 写 `var/` 冒充 HOME |
| 日志 | `TRIM_PKGVAR/logs/`、`TRIM_PKGVAR/info.log` | 应用用户 | 含 Cookie/`TRIM_API_TOKEN`/明文密码 |

> 契约 §3.2 只枚举了 `var/` 的主要子目录；当前实现还会在 `var/` 下产生 `bin/`（uv）、`pythons/`（Python 运行时）、`.uv-cache/`、`lib/`（libatomic）、`workspace/`、`skills/`、`plugins/`、`desktop-plugins/`。这些属**允许的运行期数据**；离线布局检查只覆盖**包结构**、不扫描运行期 `var/`（见 §8 第 3 项），真机抽查时以此为准。

---

## 6. 当前 → 目标：偏差与迁移

### 6.1 偏差清单（重构完成后状态）

> 结构与配置修正已落地；真机安装验收（e5）与默认入口产品选择仍需确认。

| # | 条目 | 目标 | 当前状态 |
|---|---|---|---|
| 1 | 旧适配层 | 删除 `gateway_proxy.py`/`fnos_bridge.py` | ✅ 已删除（`cmd/` 仅剩 9 脚本 + `pause_gateway.py`） |
| 2 | 运行时库脚本 | `cmd/libs.sh` → `app/runtime/lib/libs.sh` | ✅ 已迁移 |
| 3 | 新适配层 | `app/runtime/gateway/{gateway_adapter.py,gateway.conf}` | ✅ 已创建 |
| 4 | `gateway.conf` 单一来源 | `cmd/main`/adapter/`app/ui/config` 三方一致 | ✅ `cmd/main` 读取 `gateway.conf`；契约检查 exit 0 |
| 5 | `manifest.platform` | `x86` | ✅ 已改 |
| 6 | `manifest.micro_app` | 移除 | ✅ 已移除 |
| 7 | `manifest.version` | 构建注入（契约示例 `0.22.13`） | ⏳ 源文件仍 `0.22.6`，由 `build.sh` 的 `APP_VERSION` 覆盖 |
| 8 | `manifest.os_min_version` | 不写（待真机） | ✅ 未写 |
| 9 | `config/resource` | 仅 `data-share` | ✅ 已声明 `hermes-desktop` 应用文件 |
| 10 | 配置落点 | `config.yaml`/`access-info.txt` 写 `TRIM_PKGETC` | ✅ etc 实体 + `var/config.yaml -> ../etc/config.yaml`；install/main/upgrade 均迁移 |
| 11 | `access-info.txt` | `etc/`（0600） | ✅ 已写 etc 0600 |
| 12 | `app/ui/config` | 单内嵌入口（title=Hermes）、网关字段一致 | ✅ 一致（布局/契约检查器校验；`accessPerm=readonly`） |
| 13 | `cmd/main` | 启动 adapter、读 `gateway.conf` | ✅ 从 `target/runtime/gateway/` 启动；健康自检；无 9121/launch 死代码 |
| 14 | `app/webapp/` | 构建注入、不入库 | ✅ 已 gitignore；`build.sh` 每次 `rm -rf` 后复制 |
| 15 | 检查脚本 | `check_fpk_layout.py` + `check_gateway_contract.py` | ✅ 均已交付；模板/组装模式均 exit 0 |
| 16 | `build.sh` 门禁 | `py_compile` + 两道 `run_gate` | ✅ 已加，当前树可过 |
| 17 | 包根整洁 | 仅白名单条目 | ✅ 合规 |
| 18 | 单测 | `tests/` 可跑 | ✅ 73 项（Windows 12 个 AF_UNIX skip；e5 全量 OK） |

### 6.2 剩余迁移步骤（在 e5 上执行，命令可复制）

> 结构调整与落点迁移已完成；下面是**重构后**的验证/安装步骤（可直接复制）。
> 旧 `var/gateway_proxy.py`/`fnos_bridge.py` 副本由 install/upgrade 自动清理，无需手工。

```bash
# 0) 前置（仓库根目录，Node 26 在 PATH）
node -v            # 期望 v26.x（上游 build-webapp.mjs 要求 >= 26）
python3 -V

# 1) 布局检查（模板模式自动放行缺失 app/webapp）
python3 scripts/check_fpk_layout.py packaging/fpk --template

# 2) 内嵌契约检查
python3 scripts/check_gateway_contract.py packaging/fpk

# 3) 单测（Linux 全量；Windows 会 skip 12 个 AF_UNIX 用例）
python3 -m unittest discover -s tests -v

# 4) 构建（APP_VERSION 按契约示例；CI/e5 上执行）
APP_VERSION=0.22.13 bash scripts/build.sh

# 5) 安装（若需 root 停下报告，不用 sudo 密码）
appcenter-cli install-fpk dist/*.fpk --env wizard.env

# 6) 安装后布局抽查（配置文件在 etc，运行数据在 var）
ls -l /var/apps/hermes-desktop/etc/               # config.yaml/access-info.txt/gateway.conf，0600
ls -l /var/apps/hermes-desktop/var/config.yaml    # -> ../etc/config.yaml
ls -l /var/apps/hermes-desktop/target/app.sock    # adapter 运行后应存在
```

> 之前的旧 `$DATA_DIR/gateway_proxy.py`/`fnos_bridge.py` 副本已由 install/upgrade 清理；
> 若从很早的版本升级，`cmd/main diagnose` 会显示 adapter 版本与两级 CSP，可确认新代码生效。

### 6.3 卸载/升级时的数据边界

- 卸载选择「清除数据」时，归档只包含配置/会话/记忆/技能授权（当前 `uninstall_callback` 已实现），**不归档** `venv/`、`src/`（可重建，避免 GB 级备份）。
- 迁移到 `etc/` 后，归档清单需同步为 `etc/config.yaml`、`etc/access-info.txt`、`var/state|sessions|backups|channel_directory.json|gateway_state.json`、`home/.config/trim-cli`。
- `pause_gateway.py` 保留在 `cmd/`：卸载阶段 `target/` 可能已被删除，不能再依赖 `target/` 下的路径。

---

## 7. 允许 / 禁止文件清单（可直接用于布局检查）

> 本节清单与 `scripts/check_fpk_layout.py` 的常量一一对应（`ROOT_ALLOWED`、`APP_ALLOWED_DIRS`、
> `CMD_BANNED`、`MANIFEST_REQUIRED`/`MANIFEST_FROZEN` 等）；改清单必须两处同步，否则门禁与文档会漂移。
> 检查器输出自带稳定规则编号（`PKG-*`），评审与报告可直接引用（如 `PKG-ARCH-03`）。

### 7.1 允许（包内）

```text
package_root: manifest, ICON.PNG, ICON_256.PNG, app/, cmd/, config/, wizard/
app/:         ui/, webapp/, runtime/gateway/, runtime/lib/, plugins/, skills/
app/ui/:      config, images/icon_64.png, images/icon_256.png, [images/icon_512.png]
app/runtime/: gateway/{gateway_adapter.py, gateway.conf}, lib/libs.sh
cmd/:         main, install_init, install_callback, upgrade_init, upgrade_callback,
              uninstall_init, uninstall_callback, config_init, config_callback, pause_gateway.py
config/:      privilege, resource
wizard/:      install, config, upgrade, uninstall
```

### 7.2 禁止（包内任何层级）

```text
README*  docs/  tests/  scripts/  .github/  .gitignore  .upstream-sha
__pycache__/  *.pyc  .venv/  venv/  node_modules/  dist/  .build/  .build-smoke/
*.bak  *.orig  *.rej  *.log  *.pid  *.sock  *.fpk  *.tar.gz
.DS_Store  Thumbs.db  .vscode/  .idea/
```

### 7.3 检查方法（无需 QA 脚本也能跑）

```bash
# 包根白名单（输出应为空）
find packaging/fpk -mindepth 1 -maxdepth 1 \
  ! -name manifest ! -name ICON.PNG ! -name ICON_256.PNG \
  ! -name app ! -name cmd ! -name config ! -name wizard

# 禁止项（输出应为空）
find packaging/fpk \( -name '__pycache__' -o -name '*.pyc' -o -name '*.bak' \
  -o -name '*.log' -o -name '*.pid' -o -name '*.sock' -o -name 'venv' \
  -o -name 'node_modules' -o -name 'dist' -o -name '.git' \) -print
```

---

## 8. 未决点（需 Lead/真机确认，勿臆造）

| # | 问题 | 当前处理 |
|---|---|---|
| 1 | `os_min_version` 取值 | 不写，真机实测后回填 |
| 2 | adapter PID/日志文件名 | 已冻结：`var/gateway-adapter.pid`、`var/gateway-adapter.log`（见契约 §3.2） |
| 3 | `var/` 下契约未枚举的运行期目录（`bin/pythons/.uv-cache/lib/workspace/skills/plugins/desktop-plugins`） | 离线布局检查只覆盖包结构；运行期目录待真机确认 |
| 4 | `app/skills/` 内 arm64 `trim-cli` 与 `platform=x86` | 保留双架构二进制、只发 x86 包；布局检查器给出 `PKG-ARCH-03` 警告 |
| 5 | `etc/` 权限 | 已冻结：文件 0600、目录 0700（见契约 §3.2） |
| 6 | 桌面入口与直连提示 | 已按产品决定：唯一桌面入口 `Hermes`（`desktop_applaunchname=hermes-desktop.Embedded`）；直连不建图标，端口写入 `manifest.desc` |
| 7 | adapter `--log-file` | 已实现（可选；`cmd/main` 仍重定向到 `var/gateway-adapter.log`） |
| 8 | 真机安装/升级验收 | e5 已安装 0.22.6 并验证 adapter/登录/CSP/WS；升级因 `appcenter-cli` 限制未执行；`var` 符号链接缺陷已修复（见 `reports/qa-embedded.md`） |
