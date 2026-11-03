#!/usr/bin/env python3
"""Tests for scripts/check_gateway_contract.py.

Builds a minimal contract-compliant package in a temp dir and mutates one
aspect per case.  Runs on every platform (the checker is static analysis only).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_gateway_contract.py"
PREFIX = "/app/hermes-desktop"
SOCKET = "app.sock"

ENTRY_CONFIG = {
    ".url": {
        "hermes-desktop.Embedded": {
            "title": "Hermes Desktop (Embedded)",
            "icon": "images/icon_{0}.png",
            "type": "iframe",
            "gatewayPrefix": PREFIX,
            "gatewaySocket": SOCKET,
            "url": PREFIX,
            "allUsers": True,
        }
    }
}

MAIN_SCRIPT = """\
#!/bin/bash
set -u
GATEWAY_CONF="${TRIM_APPDEST}/runtime/gateway/gateway.conf"
GATEWAY_PREFIX="$(sed -n 's/^prefix=//p' "${GATEWAY_CONF}")"
GATEWAY_SOCKET_NAME="$(sed -n 's/^socket_name=//p' "${GATEWAY_CONF}")"
GATEWAY_SOCKET="${TRIM_APPDEST}/${GATEWAY_SOCKET_NAME}"
exec python3 "${TRIM_APPDEST}/runtime/gateway/gateway_adapter.py" \\
    --socket "${GATEWAY_SOCKET}" \\
    --upstream "127.0.0.1:9120" \\
    --prefix "${GATEWAY_PREFIX}"
"""

ADAPTER_STUB = """\
#!/usr/bin/env python3
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--socket", required=True)
parser.add_argument("--upstream", required=True)
parser.add_argument("--prefix", default="")
parser.add_argument("--health-path", default="/.gateway-health")
parser.add_argument("--log-file", default="")
# response handling: frame-ancestors 'none' -> 'self'
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def build_good(root: Path) -> Path:
    write(root / "app" / "runtime" / "gateway" / "gateway.conf",
          f"prefix={PREFIX}\nsocket_name={SOCKET}\n")
    write(root / "app" / "ui" / "config",
          json.dumps(ENTRY_CONFIG, ensure_ascii=False, indent=2) + "\n")
    write(root / "cmd" / "main", MAIN_SCRIPT)
    write(root / "app" / "runtime" / "gateway" / "gateway_adapter.py",
          ADAPTER_STUB)
    return root


def run_checker(target: Path):
    completed = subprocess.run(
        [sys.executable, str(CHECKER), str(target), "--json"],
        cwd=str(REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    payload = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
    return completed, payload


class GatewayContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.gettempdir()) / (
            "fpk-gw-contract-" + uuid.uuid4().hex
        )
        self.tmp.mkdir()
        self.good = build_good(self.tmp / "good")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clone(self, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(self.good, target)
        return target

    def assert_violation(self, target: Path, rule: str) -> dict:
        completed, payload = run_checker(target)
        self.assertIsNotNone(payload, completed.stderr)
        self.assertEqual(completed.returncode, 1, completed.stdout)
        rules = [item["rule"] for item in payload["violations"]]
        self.assertIn(rule, rules, payload["violations"])
        return payload

    def test_good_package_passes(self) -> None:
        completed, payload = run_checker(self.good)
        self.assertIsNotNone(payload, completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(payload["summary"]["violations"], 0)

    def test_conf_prefix_mismatch(self) -> None:
        target = self.clone("conf-mismatch")
        write(target / "app" / "runtime" / "gateway" / "gateway.conf",
              "prefix=/app/other\nsocket_name=app.sock\n")
        self.assert_violation(target, "GW-CONF-02")

    def test_missing_gateway_conf(self) -> None:
        target = self.clone("missing-conf")
        (target / "app" / "runtime" / "gateway" / "gateway.conf").unlink()
        self.assert_violation(target, "GW-CONF-01")

    def test_ui_config_prefix_mismatch(self) -> None:
        target = self.clone("entry-mismatch")
        data = json.loads(
            (target / "app" / "ui" / "config").read_text(encoding="utf-8")
        )
        data[".url"]["hermes-desktop.Embedded"]["gatewayPrefix"] = "/app/other"
        write(target / "app" / "ui" / "config",
              json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self.assert_violation(target, "GW-ENTRY-02")

    def test_main_does_not_read_gateway_conf(self) -> None:
        target = self.clone("main-no-conf")
        write(target / "cmd" / "main", "#!/bin/bash\nexit 0\n")
        self.assert_violation(target, "GW-MAIN-02")

    def test_main_uses_old_proxy(self) -> None:
        target = self.clone("main-old-proxy")
        (target / "cmd" / "main").write_text(
            "#!/bin/bash\n"
            'python3 "${TRIM_APPDEST}/cmd/gateway_proxy.py"\n',
            encoding="utf-8",
        )
        self.assert_violation(target, "GW-MAIN-04")

    def test_missing_adapter(self) -> None:
        target = self.clone("missing-adapter")
        (target / "app" / "runtime" / "gateway"
         / "gateway_adapter.py").unlink()
        self.assert_violation(target, "GW-ADAPTER-01")

    def test_usage_error_missing_directory(self) -> None:
        completed, _payload = run_checker(self.tmp / "does-not-exist")
        self.assertEqual(completed.returncode, 2)

    def test_usage_error_no_arguments(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(CHECKER)],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(completed.returncode, 2)


if __name__ == "__main__":
    unittest.main()
