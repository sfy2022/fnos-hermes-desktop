#!/usr/bin/env python3
"""Gateway contract checker for the Hermes Desktop FPK package.

Verifies the single-source-of-truth chain frozen in
``docs/REFACTOR-CONTRACT.md`` §3.1 / §5:

    app/runtime/gateway/gateway.conf  (prefix + socket_name)
        ├── app/ui/config  iframe entry (gatewayPrefix / gatewaySocket / url)
        └── cmd/main       (adapter path + prefix/socket read from gateway.conf)

plus structural checks on the adapter CLI itself.  No sockets are opened, so
this runs on every platform (including the Windows dev host and CI).

Usage:
    python scripts/check_gateway_contract.py <package_dir> [--json]

Exit codes:
    0  contract satisfied (warnings allowed)
    1  at least one contract violation
    2  usage / IO error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

APPNAME = "hermes-desktop"
EXPECTED_PREFIX = f"/app/{APPNAME}"
EXPECTED_SOCKET = "app.sock"
EXPECTED_ADAPTER = "app/runtime/gateway/gateway_adapter.py"
EXPECTED_CONF = "app/runtime/gateway/gateway.conf"
UI_CONFIG = "app/ui/config"
MAIN_SCRIPT = "cmd/main"

REQUIRED_ADAPTER_FLAGS = ("--socket", "--upstream", "--prefix")
OPTIONAL_ADAPTER_FLAGS = ("--health-path", "--log-file")


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


class ContractChecker:
    def __init__(self, root: Path):
        self.root = root
        self.findings: list = []
        self._seen: set = set()
        self.conf: dict = {}
        self.entry_map: dict = {}

    def path(self, rel: str) -> Path:
        return self.root / rel

    def add(self, rule: str, message: str, path: str | None = None,
            severity: str = "violation") -> None:
        key = (rule, path, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(Finding(rule, message, path, severity))

    def warn(self, rule: str, message: str, path: str | None = None) -> None:
        self.add(rule, message, path, severity="warning")

    def load_json(self, rel: str):
        path = self.path(rel)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            return None, f"{rel}: 无法读取: {error}"
        try:
            return json.loads(raw), None
        except json.JSONDecodeError as error:
            return None, (f"{rel}: 非法 JSON（{error.msg}，行 {error.lineno} "
                          f"列 {error.colno}）")

    # ----------------------------------------------------------------- run
    def run(self) -> int:
        self.check_gateway_conf()
        self.check_entry_config()
        self.check_main_script()
        self.check_adapter()
        violations = [item for item in self.findings
                      if item.severity == "violation"]
        return 1 if violations else 0

    def ordered(self) -> list:
        order = {"violation": 0, "warning": 1}
        return sorted(self.findings,
                      key=lambda f: (order[f.severity], f.rule,
                                     f.path or "", f.message))

    # ----------------------------------------------------------- gateway.conf
    def check_gateway_conf(self) -> None:
        path = self.path(EXPECTED_CONF)
        if not path.is_file():
            self.add("GW-CONF-01", f"缺少 {EXPECTED_CONF}", EXPECTED_CONF)
            return
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self.add("GW-CONF-01", f"{EXPECTED_CONF} 不可读: {error}",
                     EXPECTED_CONF)
            return
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                self.add(
                    "GW-CONF-01",
                    f"{EXPECTED_CONF} 第 {line_no} 行不是 key=value: "
                    f"{stripped!r}",
                    EXPECTED_CONF,
                )
                continue
            key, value = stripped.split("=", 1)
            self.conf[key.strip()] = value.strip()
        expected = {"prefix": EXPECTED_PREFIX, "socket_name": EXPECTED_SOCKET}
        for key, value in expected.items():
            actual = self.conf.get(key)
            if actual is None:
                self.add(
                    "GW-CONF-02",
                    f"{EXPECTED_CONF} 缺少 {key}（冻结值 {value!r}）",
                    EXPECTED_CONF,
                )
            elif actual != value:
                self.add(
                    "GW-CONF-02",
                    f"{EXPECTED_CONF} 的 {key}={actual!r} 冻结为 {value!r}",
                    EXPECTED_CONF,
                )

    # ------------------------------------------------------------- ui/config
    def check_entry_config(self) -> None:
        data, error = self.load_json(UI_CONFIG)
        if error:
            self.add("GW-ENTRY-01", error, UI_CONFIG)
            return
        if not isinstance(data, dict) or not isinstance(data.get(".url"), dict):
            self.add("GW-ENTRY-01", f"{UI_CONFIG} 必须是含 .url 对象的 JSON",
                     UI_CONFIG)
            return
        entries = data[".url"]
        self.entry_map = entries
        prefix = self.conf.get("prefix", EXPECTED_PREFIX)
        socket_name = self.conf.get("socket_name", EXPECTED_SOCKET)
        embedded = [
            (entry_id, entry) for entry_id, entry in entries.items()
            if isinstance(entry, dict) and entry.get("type") == "iframe"
        ]
        if not embedded:
            self.add(
                "GW-ENTRY-01",
                f"{UI_CONFIG} 缺少 iframe 内嵌入口（gatewayPrefix/"
                "gatewaySocket）",
                UI_CONFIG,
            )
            return
        for entry_id, entry in embedded:
            gateway_prefix = entry.get("gatewayPrefix")
            gateway_socket = entry.get("gatewaySocket")
            url = entry.get("url")
            if not isinstance(gateway_prefix, str) or not gateway_prefix.strip():
                self.add(
                    "GW-ENTRY-02",
                    f"入口 {entry_id!r} 缺少 gatewayPrefix",
                    UI_CONFIG,
                )
            elif gateway_prefix.strip() != prefix:
                self.add(
                    "GW-ENTRY-02",
                    f"入口 {entry_id!r} gatewayPrefix="
                    f"{gateway_prefix!r} 与 {EXPECTED_CONF} prefix={prefix!r} "
                    "不一致",
                    UI_CONFIG,
                )
            if not isinstance(gateway_socket, str) or not gateway_socket.strip():
                self.add(
                    "GW-ENTRY-02",
                    f"入口 {entry_id!r} 缺少 gatewaySocket",
                    UI_CONFIG,
                )
            elif gateway_socket != socket_name:
                self.add(
                    "GW-ENTRY-02",
                    f"入口 {entry_id!r} gatewaySocket="
                    f"{gateway_socket!r} 与 {EXPECTED_CONF} socket_name="
                    f"{socket_name!r} 不一致",
                    UI_CONFIG,
                )
            if isinstance(url, str) and isinstance(gateway_prefix, str):
                expected = gateway_prefix.strip()
                if not (url == expected or url.startswith(expected + "/")):
                    self.add(
                        "GW-ENTRY-02",
                        f"入口 {entry_id!r} url={url!r} 必须以 "
                        f"gatewayPrefix={expected!r} 开头",
                        UI_CONFIG,
                    )
            if "protocol" in entry or "port" in entry:
                self.warn(
                    "GW-ENTRY-03",
                    f"内嵌入口 {entry_id!r} 不应声明 protocol/port",
                    UI_CONFIG,
                )

    # ------------------------------------------------------------- cmd/main
    def check_main_script(self) -> None:
        path = self.path(MAIN_SCRIPT)
        if not path.is_file():
            self.add("GW-MAIN-01", f"缺少 {MAIN_SCRIPT}", MAIN_SCRIPT)
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            self.add("GW-MAIN-01", f"{MAIN_SCRIPT} 不可读: {error}", MAIN_SCRIPT)
            return

        prefix = self.conf.get("prefix", EXPECTED_PREFIX)
        socket_name = self.conf.get("socket_name", EXPECTED_SOCKET)

        if "gateway.conf" not in text:
            self.add(
                "GW-MAIN-02",
                f"{MAIN_SCRIPT} 未读取 {EXPECTED_CONF}；prefix/socket 必须以 "
                "gateway.conf 为单一来源",
                MAIN_SCRIPT,
            )
        if "gateway_adapter.py" not in text:
            self.add(
                "GW-MAIN-03",
                f"{MAIN_SCRIPT} 未引用 app/runtime/gateway/gateway_adapter.py",
                MAIN_SCRIPT,
            )
        for banned in ("gateway_proxy.py", "fnos_bridge.py"):
            if banned in text:
                self.add(
                    "GW-MAIN-04",
                    f"{MAIN_SCRIPT} 仍引用已删除的旧代理: {banned}",
                    MAIN_SCRIPT,
                )
        literal = re.search(r'GATEWAY_PREFIX\s*=\s*"([^"$()]*)"', text)
        # An empty assignment is just an initializer before load_gateway_conf();
        # only a non-empty hardcoded value can contradict gateway.conf.
        if literal and literal.group(1).strip() and literal.group(1) != prefix:
            self.add(
                "GW-MAIN-05",
                f"{MAIN_SCRIPT} 硬编码 GATEWAY_PREFIX="
                f"{literal.group(1)!r} 与 gateway.conf prefix={prefix!r} 不一致",
                MAIN_SCRIPT,
            )
        if socket_name not in text and "socket_name" not in text:
            self.add(
                "GW-MAIN-05",
                f"{MAIN_SCRIPT} 未使用 gateway.conf socket_name="
                f"{socket_name!r}",
                MAIN_SCRIPT,
            )
        if "TRIM_APPDEST" not in text:
            self.add(
                "GW-MAIN-05",
                f"{MAIN_SCRIPT} 未使用 TRIM_APPDEST 定位 adapter/socket",
                MAIN_SCRIPT,
            )
        for obsolete in ("--tcp-listen", "launch-redirect"):
            if obsolete in text:
                self.warn(
                    "GW-MAIN-06",
                    f"{MAIN_SCRIPT} 仍含待移除参数 {obsolete}（contract §task-16）",
                    MAIN_SCRIPT,
                )

    # -------------------------------------------------------------- adapter
    def check_adapter(self) -> None:
        path = self.path(EXPECTED_ADAPTER)
        if not path.is_file():
            self.add("GW-ADAPTER-01", f"缺少 {EXPECTED_ADAPTER}",
                     EXPECTED_ADAPTER)
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            self.add("GW-ADAPTER-01", f"{EXPECTED_ADAPTER} 不可读: {error}",
                     EXPECTED_ADAPTER)
            return
        missing = [flag for flag in REQUIRED_ADAPTER_FLAGS
                   if flag not in text]
        if missing:
            self.add(
                "GW-ADAPTER-02",
                f"{EXPECTED_ADAPTER} 缺少必需 CLI 参数: "
                + ", ".join(missing),
                EXPECTED_ADAPTER,
            )
        missing_optional = [flag for flag in OPTIONAL_ADAPTER_FLAGS
                            if flag not in text]
        if missing_optional:
            self.warn(
                "GW-ADAPTER-03",
                f"{EXPECTED_ADAPTER} 未实现可选 CLI 参数: "
                + ", ".join(missing_optional),
                EXPECTED_ADAPTER,
            )
        if "frame-ancestors" not in text:
            self.add(
                "GW-ADAPTER-04",
                f"{EXPECTED_ADAPTER} 未处理 frame-ancestors（内嵌必需）",
                EXPECTED_ADAPTER,
            )
        for banned in ("gateway_proxy", "fnos_bridge"):
            if banned in text:
                self.add(
                    "GW-ADAPTER-04",
                    f"{EXPECTED_ADAPTER} 仍包含已删除方案的内容: {banned}",
                    EXPECTED_ADAPTER,
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_gateway_contract.py",
        description=(
            "检查 Hermes Desktop 网关三方契约（gateway.conf / ui/config / "
            "cmd/main / adapter），规则来源 docs/REFACTOR-CONTRACT.md §3/§5"
        ),
        epilog=(
            "示例:\n"
            "  python scripts/check_gateway_contract.py packaging/fpk\n"
            "  python scripts/check_gateway_contract.py .build/fpk --json"
        ),
    )
    parser.add_argument("package_dir", help="待检查的 FPK 包根目录")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    return parser


def print_text(checker: ContractChecker, exit_code: int) -> None:
    print(f"[网关契约] dir = {checker.root}")
    findings = checker.ordered()
    if not findings:
        print("[网关契约] 未发现违规或警告。")
    for finding in findings:
        suffix = f"（{finding.path}）" if finding.path else ""
        print(f"[{finding.level}] {finding.rule}: {finding.message}{suffix}")
    violations = sum(1 for item in findings
                     if item.severity == "violation")
    warnings = len(findings) - violations
    verdict = "通过" if exit_code == 0 else "存在契约违规（阻断）"
    print(f"[网关契约] 违规 {violations} 条，警告 {warnings} 条 → "
          f"{verdict}（退出码 {exit_code}）")


def print_json(checker: ContractChecker, exit_code: int) -> None:
    findings = checker.ordered()
    payload = {
        "package_dir": str(checker.root),
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
        sys.stderr.write(f"[check_gateway_contract] ERROR: 无法解析目录: "
                         f"{error}\n")
        return 2
    if not root.exists():
        sys.stderr.write(f"[check_gateway_contract] ERROR: 目录不存在: "
                         f"{root}\n")
        return 2
    if not root.is_dir():
        sys.stderr.write(f"[check_gateway_contract] ERROR: 不是目录: {root}\n")
        return 2

    checker = ContractChecker(root)
    try:
        exit_code = checker.run()
    except OSError as error:
        sys.stderr.write(f"[check_gateway_contract] ERROR: IO 失败: {error}\n")
        return 2
    if args.json:
        print_json(checker, exit_code)
    else:
        print_text(checker, exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
