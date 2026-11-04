# 当前状态（CURRENT-STATE）

> 本页是仓库的单一事实来源，记录当前发布版本、运行布局和鉴权行为。
> 其他文档中的历史版本号、历史构建记录仅作背景，不作为当前状态依据。

## 发布版本

| 项 | 值 |
|---|---|
| 上游项目 | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) |
| 上游最新 Release | `v0.22.7` |
| 上游 commit | `b200f5bc` |
| 本仓库 Release | `v0.22.7` |
| 安装包 | `Hermes-Desktop-fnOS-v0.22.7-b200f5bc.fpk` |
| 安装包 SHA256 | `3934aba8aa31aaa411fe985392c096dc9e41ef109202f7d4a0b71c3eec2912bb` |

**版本策略**：应用版本始终与上游官方 Release 的 tag 一致，不额外递增，不单独维护应用侧版本号。

```text
上游 v0.22.7  →  应用 0.22.7
上游 v0.22.8  →  应用 0.22.8
```

## 安装与升级

- 在飞牛应用中心使用「手动安装」，选择 Release 中的 fpk。
- 已安装旧版时直接安装新版本，走 fnOS 覆盖升级，用户配置和会话保留。
- 安装向导为三步：
  1. **访问账号**：设置 Hermes 登录用户名和密码；密码留空时自动生成。
  2. **模型配置**：选择 OpenRouter / Anthropic / DeepSeek / 自定义 OpenAI 兼容服务，或稍后配置。
  3. **网络配置**：选择 GitHub 下载通道和 Python 依赖源。
- 应用中心命令行不支持升级/卸载，相关操作使用飞牛 Web UI。

## 运行布局

| 路径 | 内容 |
|---|---|
| `/var/apps/hermes-desktop/target` | 只读应用文件：`ui/`、`webapp/`、`runtime/`、`plugins/`、`skills/` |
| `/vol1/@appcenter/hermes-desktop` | `target` 的实际位置 |
| `/vol1/@appdata/hermes-desktop` | 运行数据：`venv/`、`src/`、`sessions/`、`state/`、`logs/`、`skills/` 等 |
| `/vol1/@appconf/hermes-desktop` | 指向 `@appshare` 配置的符号链接目录 |
| `/vol1/@appshare/hermes-desktop` | 应用文件（文件管理 → 应用文件可见） |
| `/vol1/@apphome/hermes-desktop` | 应用 HOME |
| `/vol1/@appmeta/hermes-desktop` | 应用元数据 |
| `/vol1/@apptemp/hermes-desktop` | 应用临时目录 |

配置实体固定在应用文件中：

```text
/vol1/@appshare/hermes-desktop/
├── config/
│   ├── config.yaml
│   ├── access-info.txt
│   └── gateway.conf
├── logs/
├── workspace/
└── data -> /vol1/@appdata/hermes-desktop
```

`@appconf` 中的三个配置文件都是指向 `@appshare` 的符号链接；`@appdata/config.yaml` 也是符号链接。修改配置时使用应用文件中的实体文件，避免编辑符号链接或旧副本。

## 桌面入口与鉴权

- 飞牛桌面只声明一个内嵌入口：`Hermes`（`type=iframe`）。
- 内嵌入口经飞牛统一网关 `/app/hermes-desktop/*` 转发到 `target/app.sock`。
- 直连 `http://<NAS_IP>:9120` 可用，但不在桌面创建第二个入口。
- 内嵌页面仍需要 Hermes 自身的用户名/密码；飞牛网关只校验 NAS 登录态，不代替应用鉴权。
- HTTP 访问时，adapter 使用 `forwarded_proto=http`，避免上游把登录 Cookie 标记为 `Secure`/`__Secure-`；否则 HTTP 浏览器会丢弃 Cookie，表现为登录后回到 `/login`。
- 内嵌登录不需要先直连 `:9120` 登录一次。

## 构建与发布

- `sync.yml` 每 6 小时检查上游最新 Release，并与 `.upstream-release` 比较。
- 只有上游发布新 Release 时才触发 `build.yml`；普通代码/文档提交不会自动发版。
- `build.yml` 只接受手动 dispatch 或 `sync.yml` 触发，按指定 Release tag 构建，不跟随上游 main。
- Release 构建成功后记录 `.upstream-release`（tag）和 `.upstream-sha`（commit）。
- 需要重建已构建版本时，手动运行 `build.yml` 并勾选 `force`。

## 常见运行问题

| 现象 | 优先检查 |
|---|---|
| 打不开 / 502 | `target/app.sock` 是否存在、adapter 是否运行、`@appdata/info.log` 与 `logs/gateway.log` |
| 9120 不监听 | `config.yaml` 的 `dashboard.basic_auth` 是否完整、`webapp.host/port` 是否正确 |
| 内嵌登录后回 `/login` | 浏览器是否残留旧的 `hermes_*` Cookie；清除该站点 Cookie 后重试 |
| 配置改了不生效 | 是否修改了 `@appshare/hermes-desktop/config/config.yaml` 实体文件 |
| 模型不工作 | 检查安装向导写入的 `model` 段和 `.env` 中的 API Key |

## 相关文档

- [安装与使用](../README.md)
- [内嵌链路与 adapter](EMBEDDING.md)
- [包结构与运行期布局](APP-STRUCTURE.md)
- [历史设计记录](IMPLEMENTATION.md)
- [内嵌契约](REFACTOR-CONTRACT.md)
