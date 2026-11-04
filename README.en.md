# Hermes Desktop for fnOS

The **official Hermes Desktop workspace** packaged for fnOS (飞牛 NAS). It uses
Hermes' own Webapp bundle, with no upstream code changes, no Electron, and no
virtual display or streaming.

**[Download the latest release](https://github.com/sfy2022/fnos-hermes-desktop/releases/latest)** ·
[Current state](docs/CURRENT-STATE.md) · [中文](README.md)

> Upstream: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
> This repository maintains the fnOS packaging, lifecycle scripts and gateway adapter.

---

## Install

1. Open [Releases](https://github.com/sfy2022/fnos-hermes-desktop/releases/latest) and download
   `Hermes-Desktop-fnOS-v<version>-<commit>.fpk` (about 22 MB, x86_64).
2. In the fnOS desktop, open **App Center → Manual install** and select the fpk.
3. The install wizard has three steps:
   - **Access account**: set the Hermes username and password; leave the password empty to generate one.
   - **Model configuration**: choose OpenRouter / Anthropic / DeepSeek / a custom OpenAI-compatible
     service, or configure it later.
   - **Network configuration**: choose the GitHub download channel and Python package index.

### App files and runtime data

| Content | Location |
|---|---|
| App files (visible in File Manager → App files) | `/vol1/@appshare/hermes-desktop` |
| Config files | `/vol1/@appshare/hermes-desktop/config` |
| Logs | `/vol1/@appshare/hermes-desktop/logs` |
| Workspace | `/vol1/@appshare/hermes-desktop/workspace` |
| Runtime data (venv, source, sessions) | `/vol1/@appdata/hermes-desktop` |
| HOME | `/vol1/@apphome/hermes-desktop` |

`/vol1/@appconf/hermes-desktop` contains symlinks to the files in `@appshare`.
Edit the real files under App files, not the symlinks.

---

## Desktop entry and direct access

| Method | Address | Authentication | Notes |
|---|---|---|---|
| Embedded desktop | fnOS gateway `/app/hermes-desktop/` | fnOS session + Hermes account | The only Hermes entry on the desktop |
| Browser direct | `http://<NAS_IP>:9120` | Hermes account | Bypasses the fnOS gateway; no second desktop entry |

Both access the same backend and the same session.

**Embedded path**: fnOS desktop iframe → fnOS gateway `/app/hermes-desktop/*` →
app `target/app.sock` → `gateway_adapter.py` → `127.0.0.1:9120`.

The adapter:

- strips the gateway prefix and injects `X-Forwarded-Prefix`;
- rewrites `Host`, `Origin` and `Referer` for the upstream;
- relays WebSocket frames;
- relaxes the page CSP from `frame-ancestors 'none'` to `'self'` and changes
  `X-Frame-Options: DENY` to `SAMEORIGIN`;
- with HTTP, removes `Secure` and the `__Secure-` prefix from authentication cookies so the
  browser does not drop them.

The response body is not rewritten. Direct `:9120` keeps the strict CSP.

Embedded sign-in uses the Hermes account configured during installation. You do **not** need to
open `:9120` and sign in first. If the embedded page keeps returning to `/login`, clear old cookies
for the NAS site and retry in a private window.

---

## What this is

The official Hermes desktop produces an Electron bundle and a Webapp bundle. This project uses the
**Webapp bundle** because it runs in a browser and does not need a display server.

Upstream `hermes_cli/webapp.py` is designed so the browser workspace and the dashboard share the
same transport, sessions and tools. This repository only packages that official build output for
fnOS, so the UI is the real Desktop workspace.

---

## Features

Same as the official Desktop Webapp:

- streaming chat, live tool activity and structured tool-call summaries
- side-by-side previews for pages, files and tool output
- file browser and Artifacts gallery
- scheduled jobs, multiple profiles and skill management
- terminal rail and Git review
- messaging gateways: Telegram, Discord, Slack, WeChat, QQ and others
- MCP integration and persistent memory
- bundled fnOS Skill for files, Docker, apps, storage, networking and monitoring

The browser form does not provide the Electron-only tray, native notifications, global shortcuts,
native file pickers, microphone access or full hardware acceleration. For those, run the official
native Desktop and connect it to the same gateway.

---

## fnOS authorization

The app includes a **fnOS authorization** panel for granting the agent access to the local NAS or
other fnOS devices.

Open it from the status bar, or go to:

```text
http://<NAS_IP>:9120/#/fnos-auth
```

Add the target NAS, start authorization, sign in with that NAS account, then paste the authorization
code back into the panel. Grant only the permissions the agent actually needs.

---

## Updates

The status bar shows the current version. When a new release is available:

1. download the new fpk and verify its SHA256;
2. install it through the native fnOS upgrade flow;
3. the app restarts while configuration and sessions are preserved.

Use the fnOS App Center Web UI for upgrades and uninstalls.

---

## Security

A signed-in user effectively has command execution on the host (terminal, file access and
credentials).

- The app binds `0.0.0.0:9120`, so Hermes requires authentication. Without a valid
  `dashboard.basic_auth` section, the webapp refuses to listen.
- Do not expose the app directly to the public internet. Use a VPN, Tailscale, or a reverse proxy
  with HTTPS.
- To change the Hermes password, write a scrypt hash to `dashboard.basic_auth.password_hash` and
  restart the app. Do not store the plaintext password in the config.

---

## Builds

Packages are built only when upstream publishes a new official Release:

| Workflow | Trigger | Purpose |
|---|---|---|
| `sync.yml` | every 6 hours / manual | Check the latest upstream Release; dispatch a build only when it differs from `.upstream-release` |
| `build.yml` | `sync.yml` / manual | Build from the upstream Release tag, publish the fpk and record the built version |

The app version always matches the upstream Release tag. To rebuild an already-built release, run
`build.yml` manually with `force`, or pass a specific `upstream_ref`.

### Local build

Requires Node.js >= 26, git, curl and fnpack:

```bash
UPSTREAM_REF=v0.22.7 APP_VERSION=0.22.7 bash scripts/build.sh
# outputs: dist/Hermes-Desktop-fnOS-v<version>-<commit>.fpk + dist/build-info.json
```

Environment overrides:

```bash
UPSTREAM_REF=v0.22.7   # upstream Release tag
APP_VERSION=0.22.7     # override the manifest version
SKIP_BUNDLE=1          # reuse an existing renderer build
OUTPUT_DIR=/path/out   # output directory
```

If `APP_VERSION` is omitted, the build uses the version of the upstream tag.

---

## Troubleshooting

### The app does not open / 502

```bash
tail -50 /vol1/@appshare/hermes-desktop/logs/gateway.log
tail -50 /vol1/@appdata/hermes-desktop/info.log
curl --unix-socket /var/apps/hermes-desktop/target/app.sock \
  http://localhost/app/hermes-desktop/.gateway-health
```

Check that the adapter is running, `target/app.sock` exists, and the prefix/socket in
`gateway.conf` match `app/ui/config`.

### Direct `:9120` does not listen

Usually `config.yaml` is missing a valid `dashboard.basic_auth`, or `webapp.host`/port is wrong.
Check the real `config/config.yaml` under App files and restart the app.

### Embedded sign-in keeps returning to `/login`

Clear old cookies for the NAS site and verify in a private window. Older sign-ins may leave
`hermes_*` cookies with different paths; the browser then sends multiple values and the server can
read an old session.

### Configuration changes have no effect

Edit the real file:

```text
/vol1/@appshare/hermes-desktop/config/config.yaml
```

Do not edit the symlink under `@appconf`, and do not edit stale copies under `@appdata`.

### Install reports that `venv` already exists

An incomplete uninstall can leave `venv` behind. Remove
`/vol1/@appdata/hermes-desktop/venv` and install again.

---

## Layout

```text
.
├── .github/workflows/          # sync / build
├── scripts/                    # build and check scripts
├── packaging/fpk/              # fpk template
│   ├── manifest
│   ├── app/
│   │   ├── ui/config           # desktop entry
│   │   ├── runtime/gateway/    # gateway adapter and gateway.conf
│   │   ├── plugins/            # bundled plugins
│   │   └── skills/             # bundled fnOS Skill
│   ├── cmd/                    # lifecycle scripts
│   ├── config/                 # privilege / resource
│   └── wizard/                 # install, config, upgrade, uninstall
├── docs/
│   ├── CURRENT-STATE.md
│   ├── EMBEDDING.md
│   ├── APP-STRUCTURE.md
│   ├── IMPLEMENTATION.md
│   └── REFACTOR-CONTRACT.md
└── README.md
```

---

## Links

- Upstream: <https://github.com/NousResearch/hermes-agent>
- Official Desktop docs: <https://hermes-agent.nousresearch.com/docs/user-guide/desktop>
- fnOS developer docs: <https://developer.fnnas.com/>
- fnpack: <https://developer.fnnas.com/docs/cli/fnpack>
- Issues: <https://github.com/sfy2022/fnos-hermes-desktop/issues>

## License

The fpk packaging and automation in this repository are a community contribution.
Upstream Hermes Agent keeps its own license (MIT).
