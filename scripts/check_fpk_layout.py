#!/usr/bin/env python3
"""Offline layout checker for the Hermes Desktop FPK package.

Rule source of truth: ``docs/REFACTOR-CONTRACT.md`` §3/§4 (frozen).
Every finding carries a stable rule id and one of two levels:

    [违规]  violation: violates the frozen contract, exit code 1
    [警告]  warning: hygiene / heuristic / template-mode allowance

Usage:
    python scripts/check_fpk_layout.py <package_dir> [--json] [--template]

Exit codes:
    0  pass: no violation (warnings allowed)
    1  at least one violation
    2  usage / IO error

Template mode:
    The source tree ``packaging/fpk`` intentionally has no ``app/webapp``
    (it is injected at build time) and may omit icons in early bootstrap.
    ``--template`` downgrades "missing app/webapp" and "missing icons" to
    warnings.  Without the flag, a tree that lacks ``app/webapp`` is
    auto-detected as a source/template tree and a ``PKG-MODE-01`` warning is
    emitted, so the same command works on the source package.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Frozen contract values (docs/REFACTOR-CONTRACT.md §3/§4)
# ---------------------------------------------------------------------------

APPNAME = "hermes-desktop"
EXPECTED_PREFIX = f"/app/{APPNAME}"
EXPECTED_SOCKET = "app.sock"
EXPECTED_SERVICE_PORT = "9120"

ROOT_ALLOWED = (
    "manifest",
    "ICON.PNG",
    "ICON_256.PNG",
    "app",
    "cmd",
    "config",
    "wizard",
)
ROOT_REQUIRED = ROOT_ALLOWED

APP_ALLOWED_DIRS = ("ui", "webapp", "runtime", "plugins", "skills")
APP_REQUIRED_DIRS = ("ui", "runtime")
APP_SOFT_DIRS = ("plugins", "skills")

CMD_SCRIPTS = (
    "main",
    "install_init",
    "install_callback",
    "upgrade_init",
    "upgrade_callback",
    "uninstall_init",
    "uninstall_callback",
    "config_init",
    "config_callback",
)
CMD_BANNED = ("gateway_proxy.py", "fnos_bridge.py")
CMD_ALLOWED_EXTRA = ("pause_gateway.py",)

WIZARD_FILES = ("install", "config", "upgrade", "uninstall")
WIZARD_ITEM_TYPES = (
    "text",
    "password",
    "radio",
    "checkbox",
    "select",
    "switch",
    "tips",
)
WIZARD_FIELD_RE = re.compile(r"^wizard_[a-z0-9_]+$")
TRIM_FIELD_RE = re.compile(r"^TRIM_")

MANIFEST_REQUIRED = (
    "appname",
    "version",
    "display_name",
    "desc",
    "source",
    "maintainer",
    "maintainer_url",
    "distributor",
    "distributor_url",
    "platform",
    "desktop_uidir",
    "desktop_applaunchname",
    "service_port",
    "checkport",
    "ctl_stop",
    "install_dep_apps",
)
MANIFEST_FROZEN = {
    "source": "thirdparty",
    "platform": "x86",
    "desktop_uidir": "ui",
    "desktop_applaunchname": f"{APPNAME}.Embedded",
    "service_port": EXPECTED_SERVICE_PORT,
    "checkport": "false",
    "ctl_stop": "true",
}
MANIFEST_BANNED = ("micro_app",)
MANIFEST_WARN_KEYS = ("os_min_version", "os_max_version")

JUNK_NAMES = {"__pycache__", ".git", "node_modules", "dist", ".DS_Store"}
JUNK_SUFFIXES = {".pyc", ".pyo", ".bak", ".orig", ".rej"}


def is_allowed_plugin_dist(relative: str) -> bool:
    """``app/plugins/<plugin>/dashboard/dist/`` is hand-written source.

    The plugin manifests reference ``dist/index.js`` / ``dist/style.css`` and
    the repo .gitignore explicitly un-ignores these files, so this ``dist``
    directory is source, not a build artifact (Lead decision, 2026-11-03).
    """
    parts = relative.split("/")
    return (
        len(parts) >= 5
        and parts[0] == "app"
        and parts[1] == "plugins"
        and parts[3] == "dashboard"
        and parts[4] == "dist"
    )


# Binary suffixes never treated as text; native magic is detected separately.
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".tif",
    ".tiff", ".zip", ".gz", ".tgz", ".tar", ".fpk", ".exe", ".dll", ".so",
    ".dylib", ".bin", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".pdf",
    ".pyc", ".pyo", ".class", ".jar", ".7z", ".xz", ".bz2",
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ELF_MACHINES = {
    3: "x86",
    40: "arm",
    62: "x86_64",
    183: "arm64",
    243: "riscv64",
}


class Finding:
    def __init__(self, rule: str, message: str, path: str | None = None,
                 severity: str = "violation"):
        self.rule = rule
        self.message = message
        self.path = path
        self.severity = severity

    @property
    def level(self) -> str:
        return "违规" if self.severity == "violation" else "警告"

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "level": self.level,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
        }


def png_size(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def native_arch(path: Path):
    """Return (format, arch) for a native binary, else None."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError:
        return None
    if len(head) < 20:
        return None
    if head[:4] == b"\x7fELF":
        endian = "little" if head[5:6] == b"\x01" else "big"
        machine = struct.unpack("<H" if endian == "little" else ">H",
                                head[18:20])[0]
        return ("elf", ELF_MACHINES.get(machine, f"elf-machine-{machine}"))
    if head[:2] == b"MZ":
        return ("pe", "pe")
    if head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        return ("macho", "macho")
    return None


class LayoutChecker:
    def __init__(self, root: Path, template_mode: bool):
        self.root = root
        self.template_mode = template_mode
        self.findings: list = []
        self._seen: set = set()
        self.manifest: dict = {}
        self.entry_map: dict = {}
        self.gateway_conf: dict = {}
        self.service_port = EXPECTED_SERVICE_PORT
        self.appname = APPNAME

    # ---------------------------------------------------------------- util
    def path(self, rel: str) -> Path:
        return self.root / rel

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def add(self, rule: str, message: str, path: str | None = None,
            severity: str = "violation") -> None:
        key = (rule, path, message, severity)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(Finding(rule, message, path, severity))

    def warn(self, rule: str, message: str, path: str | None = None) -> None:
        self.add(rule, message, path, severity="warning")

    def read_text(self, rel: str):
        try:
            data = self.path(rel).read_bytes()
        except OSError:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def load_json(self, rel: str):
        raw = self.read_text(rel)
        if raw is None:
            return None, f"{rel}: 不是合法 UTF-8 文本或文件不存在"
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as error:
            return None, (f"{rel}: 非法 JSON（{error.msg}，行 {error.lineno} "
                          f"列 {error.colno}）")

    # ----------------------------------------------------------------- run
    def run(self) -> int:
        self.check_junk()
        self.check_root()
        self.check_manifest()
        self.check_arch()
        self.check_privilege()
        self.check_resource()
        self.check_runtime()
        self.check_entry()
        self.check_wizard()
        self.check_icons()
        self.check_cmd()
        self.check_app()
        self.check_encoding()
        violations = [item for item in self.findings
                      if item.severity == "violation"]
        return 1 if violations else 0

    def ordered(self) -> list:
        order = {"violation": 0, "warning": 1}
        return sorted(self.findings,
                      key=lambda f: (order[f.severity], f.rule,
                                     f.path or "", f.message))

    # --------------------------------------------------------------- junk
    def check_junk(self) -> None:
        for path in sorted(self.root.rglob("*")):
            name = path.name
            relative = self.rel(path)
            if is_allowed_plugin_dist(relative):
                continue
            if name in JUNK_NAMES:
                self.add(
                    "PKG-JUNK-01",
                    f"包内不得包含构建/版本控制垃圾: {relative}",
                    relative,
                )
            elif path.suffix.lower() in JUNK_SUFFIXES:
                self.add(
                    "PKG-JUNK-01",
                    f"包内不得包含临时文件: {relative}",
                    relative,
                )

    # --------------------------------------------------------------- root
    def check_root(self) -> None:
        entries = {path.name: path for path in self.root.iterdir()
                   if not path.name.startswith(".")}
        for name in ROOT_REQUIRED:
            if name in entries:
                continue
            if self.template_mode and name in ("ICON.PNG", "ICON_256.PNG"):
                # Template mode allows missing icons; check_icons emits the
                # PKG-ICON-01 warning instead of a root violation.
                continue
            self.add("PKG-ROOT-01", f"包根缺少必需条目: {name}", name)
        for name, path in entries.items():
            if name not in ROOT_ALLOWED:
                self.add(
                    "PKG-ROOT-02",
                    f"包根出现未允许条目: {name}（只允许 "
                    + "/".join(ROOT_ALLOWED) + "）",
                    name,
                )
            elif path.is_dir() and name in ("ICON.PNG", "ICON_256.PNG",
                                            "manifest"):
                self.add(
                    "PKG-ROOT-02",
                    f"{name} 必须是文件而不是目录",
                    name,
                )

    # ----------------------------------------------------------- manifest
    def parse_manifest(self) -> None:
        raw = self.read_text("manifest")
        if raw is None:
            return
        for line_no, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                self.add(
                    "PKG-MANIFEST-01",
                    f"manifest 第 {line_no} 行不是 key=value: {stripped!r}",
                    "manifest",
                )
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip()
            quoted = len(value) >= 2 and value[0] == '"' and value[-1] == '"'
            if quoted:
                value = value[1:-1]
            if key in self.manifest:
                self.warn(
                    "PKG-MANIFEST-07",
                    f"manifest 键重复: {key}（后值覆盖前值）",
                    "manifest",
                )
            self.manifest[key] = value
            self.manifest_quoted = getattr(self, "manifest_quoted", {})
            self.manifest_quoted[key] = quoted

    def check_manifest(self) -> None:
        manifest_path = self.path("manifest")
        if not manifest_path.is_file():
            self.add("PKG-MANIFEST-01", "包根缺少无扩展名文件 manifest")
            return
        self.parse_manifest()
        if not self.manifest:
            self.add("PKG-MANIFEST-01", "manifest 为空或无法解析出任何键值")
            return

        missing = [key for key in MANIFEST_REQUIRED
                   if not self.manifest.get(key, "").strip()]
        if missing:
            self.add(
                "PKG-MANIFEST-02",
                "manifest 缺少必需字段或值为空: " + ", ".join(missing),
                "manifest",
            )

        raw_appname = self.manifest.get("appname", "").strip()
        if raw_appname:
            if raw_appname != APPNAME:
                self.add(
                    "PKG-MANIFEST-05",
                    f"appname 冻结为 {APPNAME!r}，实际为 {raw_appname!r}",
                    "manifest",
                )
            elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", raw_appname):
                self.add(
                    "PKG-MANIFEST-05",
                    f"appname 命名不合法: {raw_appname!r}",
                    "manifest",
                )
            self.appname = raw_appname

        for key, expected in MANIFEST_FROZEN.items():
            actual = self.manifest.get(key)
            if actual is None:
                continue  # already reported by PKG-MANIFEST-02
            if actual.strip() != expected:
                self.add(
                    "PKG-MANIFEST-03",
                    f"manifest.{key} 冻结为 {expected!r}，实际为 "
                    f"{actual!r}",
                    "manifest",
                )

        deps = self.manifest.get("install_dep_apps", "")
        if deps and not any(
            token.split(">")[0].strip() == "python312"
            for token in deps.split(":")
        ):
            self.add(
                "PKG-MANIFEST-03",
                f"install_dep_apps 必须声明 python312 依赖，实际为 {deps!r}",
                "manifest",
            )

        for key in MANIFEST_BANNED:
            if key in self.manifest:
                self.add(
                    "PKG-MANIFEST-04",
                    f"manifest 不得再声明已移除字段: {key}",
                    "manifest",
                )

        for key in MANIFEST_WARN_KEYS:
            if key in self.manifest:
                self.warn(
                    "PKG-MANIFEST-06",
                    f"manifest 暂不应写 {key}（待真机实测），实际为 "
                    f"{self.manifest[key]!r}",
                    "manifest",
                )

        for key, value in self.manifest.items():
            if getattr(self, "manifest_quoted", {}).get(key):
                continue
            if value.lower() in ("true", "false") or re.fullmatch(r"\d+", value):
                continue
            self.warn(
                "PKG-MANIFEST-07",
                f"manifest 值建议统一使用双引号: {key}={value}",
                "manifest",
            )

        # service_port is also consumed by entry checks.
        port = self.manifest.get("service_port", "").strip()
        if re.fullmatch(r"\d+", port):
            self.service_port = port

        entry = self.manifest.get("desktop_applaunchname", "").strip()
        if entry:
            self.expected_entry_id = entry

    # ---------------------------------------------------------------- arch
    def check_arch(self) -> None:
        natives = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = self.rel(path)
            # Never treat the checker's own source/JSON as binary.
            info = native_arch(path)
            if info:
                natives.append((relative, info[0], info[1]))
        platform = self.manifest.get("platform", "").strip()
        if not platform:
            return
        if platform == "all":
            if natives:
                listed = ", ".join(item[0] for item in natives[:3])
                self.add(
                    "PKG-ARCH-01",
                    f"platform=all 但包内含架构相关原生二进制 "
                    f"({len(natives)} 个): {listed}",
                    natives[0][0],
                )
            return
        if not natives:
            return
        archs = {item[2] for item in natives}
        if platform == "x86":
            if "x86_64" not in archs:
                self.add(
                    "PKG-ARCH-02",
                    f"platform=x86 但未发现 x86_64 原生二进制，只发现: "
                    + ", ".join(sorted(archs)),
                    natives[0][0],
                )
            other = sorted(
                arch for arch in archs if arch not in ("x86_64", "x86")
            )
            if other:
                self.warn(
                    "PKG-ARCH-03",
                    f"platform=x86 但包内还含其它架构二进制: "
                    + ", ".join(other) + "（确认仅随包分发 x86_64）",
                )
        elif platform == "arm":
            if not ({"arm64", "arm"} & archs):
                self.add(
                    "PKG-ARCH-02",
                    "platform=arm 但未发现 arm/arm64 原生二进制",
                    natives[0][0],
                )

    # ----------------------------------------------------------- privilege
    def check_privilege(self) -> None:
        rel = "config/privilege"
        if not self.path(rel).is_file():
            self.add("PKG-PRIV-01", "缺少 config/privilege 文件", rel)
            return
        data, error = self.load_json(rel)
        if error:
            self.add("PKG-PRIV-01", error, rel)
            return
        if not isinstance(data, dict):
            self.add("PKG-PRIV-01", f"{rel} 顶层必须是 JSON 对象", rel)
            return
        defaults = data.get("defaults")
        run_as = None
        if isinstance(defaults, dict):
            run_as = defaults.get("run-as")
        if run_as is None:
            run_as = data.get("run-as")
        if run_as != "package":
            self.add(
                "PKG-PRIV-02",
                f"{rel} 冻结为 run-as=package，实际为 {run_as!r}",
                rel,
            )
        for key in ("username", "groupname"):
            value = data.get(key)
            if value != APPNAME:
                self.add(
                    "PKG-PRIV-02",
                    f'{rel} 冻结 {key}="{APPNAME}"，实际为 {value!r}',
                    rel,
                )
        if "join-groups" in data:
            self.warn(
                "PKG-PRIV-02",
                f"{rel} 冻结为无 join-groups，实际声明了 "
                f"{data.get('join-groups')!r}",
                rel,
            )

    # ------------------------------------------------------------ resource
    def check_resource(self) -> None:
        rel = "config/resource"
        if not self.path(rel).is_file():
            self.add("PKG-RES-01", "缺少 config/resource 文件", rel)
            return
        data, error = self.load_json(rel)
        if error:
            self.add("PKG-RES-01", error, rel)
            return
        if not isinstance(data, dict):
            self.add("PKG-RES-01", f"{rel} 顶层必须是 JSON 对象", rel)
            return
        if data:
            self.add(
                "PKG-RES-02",
                f"{rel} 冻结为 {{}}（不声明共享目录/网关/Docker 资源），"
                "实际声明了: " + ", ".join(sorted(data)),
                rel,
            )

    # --------------------------------------------------------------- entry
    def check_entry(self) -> None:
        rel = f"app/{self.manifest.get('desktop_uidir', 'ui').strip() or 'ui'}" \
              "/config"
        if not self.path(rel).is_file():
            self.add("PKG-ENTRY-01", f"缺少桌面入口配置 {rel}", rel)
            return
        data, error = self.load_json(rel)
        if error:
            self.add("PKG-ENTRY-01", error, rel)
            return
        if not isinstance(data, dict) or not isinstance(data.get(".url"), dict):
            self.add("PKG-ENTRY-01", f"{rel} 必须是含 .url 对象的 JSON",
                     rel)
            return
        entries = data[".url"]
        self.entry_map = entries
        if not entries:
            self.add("PKG-ENTRY-01", f"{rel} 的 .url 中没有任何入口", rel)
            return

        appname = self.appname
        has_embedded = False
        has_direct = False
        for entry_id, entry in entries.items():
            if not isinstance(entry, dict):
                self.add(
                    "PKG-ENTRY-02",
                    f"入口 {entry_id!r} 的值必须是 JSON 对象",
                    rel,
                )
                continue
            for field in ("title", "icon", "type", "url"):
                value = entry.get(field)
                if value is None or (isinstance(value, str)
                                     and not value.strip()):
                    self.add(
                        "PKG-ENTRY-02",
                        f"入口 {entry_id!r} 缺少必需字段或值为空: {field}",
                        rel,
                    )
            if not entry_id.startswith(appname):
                self.add(
                    "PKG-ENTRY-03",
                    f"入口 ID {entry_id!r} 必须以 appname {appname!r} 为前缀",
                    rel,
                )
            entry_type = entry.get("type")
            self.check_entry_icon(entry_id, entry.get("icon"), rel)
            if entry_type == "iframe":
                has_embedded = True
                self.check_embedded_entry(entry_id, entry, rel)
            elif entry_type == "url":
                has_direct = True
                self.check_direct_entry(entry_id, entry, rel)
            elif entry_type is not None:
                self.warn(
                    "PKG-ENTRY-02",
                    f"入口 {entry_id!r} 的 type 应为 iframe 或 url，"
                    f"实际为 {entry_type!r}",
                    rel,
                )
            all_users = entry.get("allUsers")
            if all_users is not None and not isinstance(all_users, bool):
                self.warn(
                    "PKG-ENTRY-09",
                    f"入口 {entry_id!r} 的 allUsers 必须为布尔值",
                    rel,
                )

        if not has_embedded:
            self.add(
                "PKG-ENTRY-06",
                "缺少 iframe 内嵌入口（gatewayPrefix/gatewaySocket），"
                "桌面内嵌链路无法建立",
                rel,
            )
        # 产品决定：桌面只保留内嵌图标，直连仅通过 manifest.desc 提示端口，
        # 因此不再要求 ui/config 里必须有 type=url 的直连入口。

        expected_id = getattr(self, "expected_entry_id", "")
        if expected_id and expected_id not in entries:
            self.add(
                "PKG-ENTRY-10",
                f"manifest.desktop_applaunchname={expected_id!r} 未在 "
                f"{rel} 的 .url 中定义",
                "manifest",
            )

    def check_entry_icon(self, entry_id: str, icon, rel: str) -> None:
        if not isinstance(icon, str) or not icon.strip():
            return
        if icon.startswith("/") or ".." in icon.split("/") or "\\" in icon:
            self.add(
                "PKG-ENTRY-04",
                f"入口 {entry_id!r} 的 icon 必须是相对 UI 目录的安全路径: "
                f"{icon!r}",
                rel,
            )
            return
        uidir = self.manifest.get("desktop_uidir", "ui").strip() or "ui"
        targets = []
        if "{0}" in icon:
            targets = [f"app/{uidir}/{icon.replace('{0}', str(size))}"
                       for size in (64, 256)]
        else:
            targets = [f"app/{uidir}/{icon}"]
        missing = [target for target in targets if not self.path(target).is_file()]
        if not missing:
            return
        message = (f"入口 {entry_id!r} 的 icon 引用文件不存在: "
                   + ", ".join(missing))
        if self.template_mode:
            self.warn("PKG-ENTRY-04", message, rel)
        else:
            self.add("PKG-ENTRY-04", message, rel)

    def check_embedded_entry(self, entry_id: str, entry: dict, rel: str) -> None:
        prefix = entry.get("gatewayPrefix")
        socket_name = entry.get("gatewaySocket")
        url = entry.get("url")
        expected_prefix = f"/app/{self.appname}"
        if not isinstance(prefix, str) or not prefix.strip():
            self.add(
                "PKG-ENTRY-05",
                f"内嵌入口 {entry_id!r} 缺少 gatewayPrefix",
                rel,
            )
        elif prefix.strip() != expected_prefix:
            self.add(
                "PKG-ENTRY-05",
                f"内嵌入口 {entry_id!r} 的 gatewayPrefix 应为 "
                f"{expected_prefix!r}，实际为 {prefix!r}",
                rel,
            )
        if not isinstance(socket_name, str) or not socket_name.strip():
            self.add(
                "PKG-ENTRY-05",
                f"内嵌入口 {entry_id!r} 缺少 gatewaySocket",
                rel,
            )
        elif "/" in socket_name or "\\" in socket_name:
            self.add(
                "PKG-ENTRY-05",
                f"内嵌入口 {entry_id!r} 的 gatewaySocket 只能是文件名，"
                f"实际为 {socket_name!r}",
                rel,
            )
        else:
            expected_socket = self.gateway_conf.get("socket_name") \
                or EXPECTED_SOCKET
            if socket_name != expected_socket:
                self.add(
                    "PKG-ENTRY-05",
                    f"内嵌入口 {entry_id!r} 的 gatewaySocket="
                    f"{socket_name!r} 与 gateway.conf socket_name="
                    f"{expected_socket!r} 不一致",
                    rel,
                )
        if isinstance(url, str) and isinstance(prefix, str) and prefix.strip():
            expected = prefix.strip()
            if not (url == expected or url.startswith(expected + "/")):
                self.add(
                    "PKG-ENTRY-05",
                    f"内嵌入口 {entry_id!r} 的 url={url!r} 必须以 "
                    f"gatewayPrefix={expected!r} 开头",
                    rel,
                )
        if "protocol" in entry or "port" in entry:
            self.warn(
                "PKG-ENTRY-08",
                f"内嵌入口 {entry_id!r} 不应声明 protocol/port（网关入口忽略）",
                rel,
            )

    def check_direct_entry(self, entry_id: str, entry: dict, rel: str) -> None:
        port = entry.get("port")
        if port is None:
            self.warn(
                "PKG-ENTRY-07",
                f"直连入口 {entry_id!r} 未声明 port（冻结值为 "
                f"{self.service_port}）",
                rel,
            )
            return
        if str(port).strip() != self.service_port:
            self.add(
                "PKG-ENTRY-07",
                f"直连入口 {entry_id!r} 的 port={port!r} 与 manifest."
                f"service_port={self.service_port!r} 不一致",
                rel,
            )

    # -------------------------------------------------------------- wizard
    def check_wizard(self) -> None:
        if not self.path("wizard").is_dir():
            self.add("PKG-WIZARD-01", "缺少 wizard/ 目录", "wizard")
            return
        for name in WIZARD_FILES:
            rel = f"wizard/{name}"
            if not self.path(rel).is_file():
                self.add(
                    "PKG-WIZARD-01",
                    f"缺少向导文件 {rel}（contract §3.1 列出四份）",
                    rel,
                )
                continue
            data, error = self.load_json(rel)
            if error:
                self.add("PKG-WIZARD-02", error, rel)
                continue
            if not isinstance(data, list):
                self.add("PKG-WIZARD-02", f"{rel} 顶层必须是 JSON 数组", rel)
                continue
            for step_index, step in enumerate(data):
                if not isinstance(step, dict):
                    self.add(
                        "PKG-WIZARD-02",
                        f"{rel}[{step_index}] 必须是 JSON 对象",
                        rel,
                    )
                    continue
                title = step.get("stepTitle")
                if not isinstance(title, str) or not title.strip():
                    self.add(
                        "PKG-WIZARD-02",
                        f"{rel}[{step_index}] 缺少 stepTitle 字符串",
                        rel,
                    )
                items = step.get("items")
                if not isinstance(items, list):
                    self.add(
                        "PKG-WIZARD-02",
                        f"{rel}[{step_index}] 缺少 items 数组",
                        rel,
                    )
                    continue
                for item_index, item in enumerate(items):
                    self.check_wizard_item(rel, step_index, item_index, item)

    def check_wizard_item(self, rel: str, step_index: int,
                          item_index: int, item) -> None:
        prefix = f"{rel}[{step_index}].items[{item_index}]"
        if not isinstance(item, dict):
            self.add("PKG-WIZARD-03", f"{prefix} 必须是 JSON 对象", rel)
            return
        item_type = item.get("type")
        if item_type not in WIZARD_ITEM_TYPES:
            self.add(
                "PKG-WIZARD-03",
                f"{prefix}.type 必须为 {'/'.join(WIZARD_ITEM_TYPES)} 之一，"
                f"实际为 {item_type!r}",
                rel,
            )
        if item_type == "tips":
            return
        field = item.get("field")
        if not isinstance(field, str) or not field.strip():
            self.add(
                "PKG-WIZARD-04",
                f"{prefix} 缺少 field（除 tips 外每个表单项都必须声明 field）",
                rel,
            )
            return
        if TRIM_FIELD_RE.match(field):
            self.add(
                "PKG-WIZARD-04",
                f"{prefix}.field 不得使用保留前缀 TRIM_: {field!r}",
                rel,
            )
        elif not WIZARD_FIELD_RE.fullmatch(field):
            self.add(
                "PKG-WIZARD-04",
                f"{prefix}.field 必须以 wizard_ 开头，实际为 {field!r}",
                rel,
            )
        if item_type == "password":
            for key in ("initValue", "default"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    self.warn(
                        "PKG-WIZARD-05",
                        f"{prefix}.{key} 含明文默认值，密码应由用户填写或随机生成",
                        rel,
                    )

    # --------------------------------------------------------------- icons
    def check_icons(self) -> None:
        root_icons = {"ICON.PNG": 64, "ICON_256.PNG": 256}
        ui_icons = {
            f"app/{self.manifest.get('desktop_uidir', 'ui').strip() or 'ui'}"
            "/images/icon_64.png": 64,
            f"app/{self.manifest.get('desktop_uidir', 'ui').strip() or 'ui'}"
            "/images/icon_256.png": 256,
        }
        for rel, expected in {**root_icons, **ui_icons}.items():
            self.check_icon(rel, expected)
        uidir = self.manifest.get("desktop_uidir", "ui").strip() or "ui"
        optional_512 = f"app/{uidir}/images/icon_512.png"
        if self.path(optional_512).is_file():
            self.check_icon(optional_512, 512, optional=True)

    def check_icon(self, rel: str, expected: int, optional: bool = False) -> None:
        path = self.path(rel)
        if not path.is_file():
            message = f"缺少图标 {rel}（{expected}x{expected}）"
            if self.template_mode:
                self.warn("PKG-ICON-01", message, rel)
            else:
                self.add("PKG-ICON-01", message, rel)
            return
        info = png_size(path)
        if info is None:
            self.add("PKG-ICON-01", f"{rel} 不是合法 PNG（签名/IHDR 失败）",
                     rel)
            return
        width, height = info
        if (width, height) != (expected, expected):
            message = (f"{rel} 尺寸应为 {expected}x{expected}，"
                       f"实际为 {width}x{height}")
            if optional:
                self.warn("PKG-ICON-03", message, rel)
            else:
                self.add("PKG-ICON-01", message, rel)

    # ----------------------------------------------------------------- cmd
    def check_cmd(self) -> None:
        if not self.path("cmd").is_dir():
            self.add("PKG-CMD-01", "缺少 cmd/ 目录", "cmd")
            return
        entries = {path.name: path for path in self.path("cmd").iterdir()}
        for name in CMD_SCRIPTS:
            if name not in entries or not entries[name].is_file():
                self.add(
                    "PKG-CMD-02",
                    f"cmd/ 缺少生命周期脚本: {name}",
                    f"cmd/{name}",
                )
        for name in CMD_BANNED:
            if name in entries:
                self.add(
                    "PKG-CMD-03",
                    f"cmd/{name} 必须删除（contract §1 移除旧代理方案）",
                    f"cmd/{name}",
                )
        for name in CMD_ALLOWED_EXTRA:
            if name not in entries:
                self.warn(
                    "PKG-CMD-04",
                    f"cmd/ 缺少内部辅助脚本: {name}（卸载期需要）",
                    f"cmd/{name}",
                )
        allowed = set(CMD_SCRIPTS) | set(CMD_ALLOWED_EXTRA)
        for name in entries:
            if name in allowed:
                continue
            self.warn(
                "PKG-CMD-05",
                f"cmd/ 出现未允许的额外文件: {name}"
                "（libs.sh 应移至 app/runtime/lib/）",
                f"cmd/{name}",
            )

    # ----------------------------------------------------------------- app
    def check_app(self) -> None:
        app_dir = self.path("app")
        if not app_dir.is_dir():
            self.add("PKG-APP-01", "缺少 app/ 目录", "app")
            return
        entries = {path.name: path for path in app_dir.iterdir()}
        for name, path in entries.items():
            if name not in APP_ALLOWED_DIRS:
                self.add(
                    "PKG-APP-02",
                    f"app/ 下出现未允许条目: {name}（只允许 "
                    + "/".join(APP_ALLOWED_DIRS) + "）",
                    f"app/{name}",
                )
                continue
            if not path.is_dir():
                self.add(
                    "PKG-APP-02",
                    f"app/{name} 必须是目录",
                    f"app/{name}",
                )
        for name in APP_REQUIRED_DIRS:
            if name not in entries:
                self.add(
                    "PKG-APP-03",
                    f"app/ 缺少必需子目录: {name}",
                    f"app/{name}",
                )
        for name in APP_SOFT_DIRS:
            if name not in entries:
                self.warn(
                    "PKG-APP-03",
                    f"app/ 缺少随包目录: {name}（确认当前发布不需要）",
                    f"app/{name}",
                )
        if "webapp" not in entries:
            message = ("缺少 app/webapp/（构建注入的 Webapp 产物目录）")
            if self.template_mode:
                self.warn("PKG-APP-04", message, "app/webapp")
            else:
                self.add("PKG-APP-04", message, "app/webapp")

    # ------------------------------------------------------------- runtime
    def check_runtime(self) -> None:
        gateway = self.path("app/runtime/gateway")
        adapter = gateway / "gateway_adapter.py"
        conf = gateway / "gateway.conf"
        if not adapter.is_file():
            self.add(
                "PKG-RUNTIME-01",
                "缺少 app/runtime/gateway/gateway_adapter.py",
                "app/runtime/gateway/gateway_adapter.py",
            )
        if not conf.is_file():
            self.add(
                "PKG-RUNTIME-02",
                "缺少 app/runtime/gateway/gateway.conf",
                "app/runtime/gateway/gateway.conf",
            )
        else:
            self.parse_gateway_conf(conf)
        libs = self.path("app/runtime/lib/libs.sh")
        if not libs.is_file():
            self.warn(
                "PKG-RUNTIME-03",
                "缺少 app/runtime/lib/libs.sh（应从 cmd/libs.sh 移入）",
                "app/runtime/lib/libs.sh",
            )

    def parse_gateway_conf(self, path: Path) -> None:
        rel = self.rel(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self.add("PKG-RUNTIME-02", f"{rel} 不是合法 UTF-8 文本: {error}",
                     rel)
            return
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                self.add(
                    "PKG-RUNTIME-02",
                    f"{rel} 第 {line_no} 行不是 key=value: {stripped!r}",
                    rel,
                )
                continue
            key, value = stripped.split("=", 1)
            self.gateway_conf[key.strip()] = value.strip()
        expected = {"prefix": EXPECTED_PREFIX, "socket_name": EXPECTED_SOCKET}
        for key, value in expected.items():
            actual = self.gateway_conf.get(key)
            if actual is None:
                self.add(
                    "PKG-RUNTIME-02",
                    f"{rel} 缺少 {key}",
                    rel,
                )
            elif actual != value:
                self.add(
                    "PKG-RUNTIME-02",
                    f"{rel} 的 {key}={actual!r} 冻结为 {value!r}",
                    rel,
                )

    # ------------------------------------------------------------ encoding
    def check_encoding(self) -> None:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = self.rel(path)
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            if native_arch(path):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
                self.add(
                    "PKG-ENC-01",
                    f"{relative} 含 BOM；包内文本必须 UTF-8 无 BOM",
                    relative,
                )
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                self.add(
                    "PKG-ENC-01",
                    f"{relative} 不是合法 UTF-8（字节偏移 {error.start}）",
                    relative,
                )
                continue
            if b"\r" in data:
                self.add(
                    "PKG-ENC-02",
                    f"{relative} 含 CR/CRLF；包内文本必须仅 LF 换行",
                    relative,
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_fpk_layout.py",
        description=(
            "离线检查 Hermes Desktop FPK 包布局（规则来源："
            "docs/REFACTOR-CONTRACT.md §3/§4）"
        ),
        epilog=(
            "示例:\n"
            "  python scripts/check_fpk_layout.py packaging/fpk --template\n"
            "  python scripts/check_fpk_layout.py dist/unpacked --json"
        ),
    )
    parser.add_argument("package_dir", help="待检查的 FPK 包根目录")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument(
        "--template",
        action="store_true",
        help="模板/源码包模式：允许缺 app/webapp，缺图标降级为警告",
    )
    return parser


def print_text(checker: LayoutChecker, exit_code: int) -> None:
    print(f"[布局检查] dir = {checker.root}")
    print(f"[布局检查] 模式 = {'模板/源码包' if checker.template_mode else '完整包'}")
    findings = checker.ordered()
    if not findings:
        print("[布局检查] 未发现违规或警告。")
    for finding in findings:
        suffix = f"（{finding.path}）" if finding.path else ""
        print(f"[{finding.level}] {finding.rule}: {finding.message}{suffix}")
    violations = sum(1 for item in findings
                     if item.severity == "violation")
    warnings = len(findings) - violations
    verdict = "通过" if exit_code == 0 else "存在布局违规（阻断）"
    print(f"[布局检查] 违规 {violations} 条，警告 {warnings} 条 → "
          f"{verdict}（退出码 {exit_code}）")


def print_json(checker: LayoutChecker, exit_code: int) -> None:
    findings = checker.ordered()
    payload = {
        "package_dir": str(checker.root),
        "template_mode": checker.template_mode,
        "violations": [item.as_dict() for item in findings
                       if item.severity == "violation"],
        "warnings": [item.as_dict() for item in findings
                     if item.severity == "warning"],
        "summary": {
            "violations": sum(1 for item in findings
                              if item.severity == "violation"),
            "warnings": sum(1 for item in findings
                            if item.severity == "warning"),
            "exit_code": exit_code,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.package_dir)
    try:
        root = root.resolve()
    except OSError as error:
        sys.stderr.write(f"[check_fpk_layout] ERROR: 无法解析目录: {error}\n")
        return 2
    if not root.exists():
        sys.stderr.write(f"[check_fpk_layout] ERROR: 目录不存在: {root}\n")
        return 2
    if not root.is_dir():
        sys.stderr.write(f"[check_fpk_layout] ERROR: 不是目录: {root}\n")
        return 2

    template_mode = args.template
    auto_note = None
    if not template_mode and not (root / "app" / "webapp").is_dir():
        template_mode = True
        auto_note = (
            "未发现 app/webapp（构建注入目录），按模板/源码包模式检查；"
            "完整包请显式传入完整构建产物目录"
        )

    checker = LayoutChecker(root, template_mode)
    if auto_note:
        checker.warn("PKG-MODE-01", auto_note)
    try:
        exit_code = checker.run()
    except OSError as error:
        sys.stderr.write(f"[check_fpk_layout] ERROR: IO 失败: {error}\n")
        return 2

    if args.json:
        print_json(checker, exit_code)
    else:
        print_text(checker, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
