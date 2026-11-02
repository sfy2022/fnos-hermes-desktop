# Hermes Desktop for fnOS

The **official Hermes Desktop workspace** packaged for fnOS (飞牛 NAS) — using
Hermes' own browser-hosted "Webapp" form. **No upstream code is modified**, no
Electron, no virtual display, no VNC streaming.

[中文](README.md) · [Implementation report](docs/IMPLEMENTATION.md)

> Packaged by [巴盟女婿](https://github.com/sfy2022/fnos-hermes-desktop)
> Upstream: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)

---

## What this is

The Hermes desktop app (Electron) produces its renderer in **two forms**:

| Artifact | Purpose | Here |
|---|---|---|
| Electron bundle (AppImage/deb/rpm) | Native desktop client | ✗ Upstream Linux release legs are disabled; NAS has no display |
| **Webapp bundle** (`dist-webapp`) | **The Desktop workspace, in a browser** | ✅ Used |

Upstream's `hermes_cli/webapp.py` states the intent:

> *Build and launch plumbing for the browser-hosted **Desktop workspace**. The browser surface
> **shares Hermes' hardened dashboard server**... it never creates a second HTTP/WebSocket stack.*

In other words, the official Desktop UI and the browser dashboard **share the same
transport layer, sessions and tools**. This project packages that official build
output as an fnOS application, so the NAS desktop icon opens the real thing.

**Result:** you get the **unmodified official Desktop interface** on the NAS — not a lookalike.

---

## Features

Identical to the official Desktop Webapp:

- **Chat** — streaming responses, live tool activity, structured tool-call summaries
- **Side-by-side previews** — web pages, files and tool output in one pane
- **File browser** — explore the working directory
- **Artifacts** — searchable gallery of session outputs
- **Scheduled jobs**, **Profiles**, **Skills management**
- **Terminal rail**, **Git review**
- **Messaging gateways** — WeChat / QQ / Lark / Telegram / Discord / Slack …
- **MCP integration**, **persistent memory**

Shares one session and one queue with the CLI, the native Desktop and the gateway.

### What the browser form cannot do (vs. Electron)

- Global shortcuts, always-on-top overlays, external-terminal launch
- `safeStorage`-encrypted credential storage
- Microphone / voice, system notifications, clipboard read
  (browsers disable these on a plain-HTTP origin that isn't loopback)

If you need those, run the native Desktop on Windows/macOS and point it at the
same gateway in **remote mode**.

---

## Install

飞牛桌面 → **应用中心** → **手动安装** → choose `hermes-desktop.fpk`

The wizard asks for:

1. **Username / password** (leave the password blank and a random 20-character
   one is generated; it is written to `access-info.txt` in the app data dir)
2. **GitHub channel** (auto-probe by default) and **Python index**
   (official PyPI by default)

Then:

1. The **Hermes Desktop** icon appears on the desktop — click it to open in a
   fnOS window
2. First launch walks you through picking a model provider
3. To let the agent operate the NAS, feed it the
   [official fnOS Skill](https://club.fnnas.com/forum.php?mod=viewthread&tid=67918)

---

## Security

⚠️ **A signed-in user is effectively granted command execution on the host**
(terminal tools, file access, credentials).

- The app binds `0.0.0.0:9120` and **always enforces password auth**
  (a wildcard bind engages the dashboard sign-in gate)
- **Never expose it to the public internet.** Use a VPN / Tailscale, or a
  reverse proxy with HTTPS
- Change the password: edit `dashboard.basic_auth` in the app's `config.yaml`,
  then restart the app

---

## Automated builds

GitHub Actions keeps this in sync with upstream and publishes ready-to-install
packages:

| Workflow | Trigger | Purpose |
|---|---|---|
| `sync.yml` | every 6 hours | Compare upstream commit; build only when it changed |
| `build.yml` | daily 03:17 / manual / script changes | Clone upstream → build renderer → pack fpk → publish Release |

Grab `hermes-desktop.fpk` from the **Releases** page.

### Build locally

Requires **Node >= 26** (a hard check in upstream's build script) plus git and curl:

```bash
bash scripts/build.sh
# outputs: dist/hermes-desktop.fpk + dist/build-info.json
```

Environment overrides:

```bash
UPSTREAM_REF=v0.22.5   # build a specific upstream version
SKIP_BUNDLE=1          # reuse an existing renderer build
OUTPUT_DIR=/path/out   # where to write artifacts
```

A fast check that the packaging chain is intact (~1 minute, no full renderer build):

```bash
bash scripts/smoke.sh
```

---

## Technical notes (hard-won constraints)

Each of these was found by running on a real NAS, and each is baked into
`scripts/build.sh` and `packaging/fpk/cmd/`.

### Build side

- **Node must be >= 26** — upstream's `scripts/build/node-deps.mjs` enforces it
- The renderer build must skip the Electron binary
  (`build-webapp.mjs` already does: `electron: false`)
- The renderer is **not committed** (47 MB / 1081 files); CI regenerates it

### Runtime side (the fpk install scripts)

1. **Install from GitHub source, not PyPI** — PyPI tops out at 0.19.0, and that
   release has **no `hermes webapp` command**
2. **Python 3.14 is required** — dependencies are declared under
   `python_version >= '3.14'` (`ruamel.yaml`) but imported unconditionally
3. **Resolve dependencies against official PyPI** with
   `--exclude-newer 2027-01-01`: modern uv defaults to an `exclude-newer`
   cutoff, and **mirrors that omit upload-time metadata** cause every
   dependency to be filtered out as "too new"
4. **Multi-path GitHub fallback** — git clone → codeload → gh-proxy.com →
   ghproxy.net → direct; `curl` needs `--http1.1` on the NAS
5. **Don't hand-assemble the uv download URL** — wheel filenames carry the
   version; resolve via the PyPI JSON API instead
6. **`TRIM_SERVICE_PORT` may be empty during install** — fall back to 9120 and
   validate it's numeric
7. **fnOS `wizard/*` files must be JSON arrays** — shell scripts break
   `fnpack build`

---

## Layout

```
.
├── .github/workflows/
│   ├── build.yml              # build + publish
│   └── sync.yml               # upstream change detection
├── scripts/
│   ├── build.sh               # full build pipeline
│   └── smoke.sh               # fast packaging check
├── packaging/fpk/             # fpk template (renderer injected at build time)
│   ├── manifest
│   ├── cmd/                   # lifecycle scripts
│   ├── config/                # privilege / resource
│   ├── wizard/                # install wizard (JSON)
│   └── app/ui/config          # desktop entry (iframe → 9120)
└── docs/IMPLEMENTATION.md     # implementation report
```

---

## Links

- Upstream: <https://github.com/NousResearch/hermes-agent>
- Official Desktop docs: <https://hermes-agent.nousresearch.com/docs/user-guide/desktop>
- fnOS developer docs: <https://developer.fnnas.com/>
- fnpack: <https://developer.fnnas.com/docs/cli/fnpack>

## License

This repository (the fpk packaging and automation) is a community contribution.
Upstream Hermes Agent keeps its own license (MIT).
