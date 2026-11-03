"""Regression tests for shell-level contracts discovered on the e5 device.

These are static checks (no bash required) so they run on Windows and Linux:
  * the etc/var config symlink must use the absolute ``CONFIG_FILE`` path —
    on fnOS, var (/vol1/@appdata/<app>) and etc (/vol1/@appconf/<app>) are NOT
    siblings, so ``../etc/config.yaml`` is broken;
  * install/upgrade/main must clean stale gateway_proxy.py/fnos_bridge.py;
  * main must start the thin adapter from target/runtime/gateway and must not
    bring back the old TCP/launch workarounds;
  * basic_auth finalization must exist after the venv is created.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CMD = REPO / "packaging" / "fpk" / "cmd"


def read(name: str) -> str:
    return (CMD / name).read_text(encoding="utf-8")


class ShellContractTests(unittest.TestCase):
    def test_config_symlink_is_absolute(self) -> None:
        for name in ("install_callback", "upgrade_callback", "main"):
            text = read(name)
            self.assertNotIn('ln -sfn "../etc/config.yaml"', text, name)
            self.assertIn('ln -sfn "${CONFIG_FILE}" "${DATA_DIR}/config.yaml"', text, name)

    def test_stale_adapter_cleanup(self) -> None:
        install = read("install_callback")
        upgrade = read("upgrade_callback")
        for text in (install, upgrade):
            self.assertIn("gateway_proxy.py", text)
            self.assertIn("fnos_bridge.py", text)

    def test_main_uses_thin_adapter(self) -> None:
        main = read("main")
        self.assertIn("runtime/gateway/gateway_adapter.py", main)
        self.assertIn("runtime/gateway/gateway.conf", main)
        self.assertNotIn("--tcp-listen", main)
        self.assertNotIn("enable-launch-redirect", main)
        self.assertNotIn("gateway_proxy.py", main)
        self.assertNotIn("fnos_bridge.py", main)

    def test_main_reads_gateway_conf(self) -> None:
        main = read("main")
        self.assertIn("load_gateway_conf", main)
        self.assertIn("GATEWAY_SOCKET_NAME", main)

    def test_basic_auth_finalized_after_install(self) -> None:
        install = read("install_callback")
        main = read("main")
        self.assertIn("finalize_webapp_auth", install)
        self.assertIn("finalize_webapp_auth", main)
        # install must call it after the venv/hermes install, not before.
        install_hermes_at = install.index("install_hermes || exit 1")
        self.assertGreater(
            install.index("finalize_webapp_auth\n", install_hermes_at),
            install_hermes_at,
        )

    def test_libs_script_relocated(self) -> None:
        install = read("install_callback")
        self.assertIn("runtime/lib/libs.sh", install)
        self.assertNotIn("cmd/libs.sh", install)


class GatewayConfigContractTests(unittest.TestCase):
    def test_ui_and_conf_agree(self) -> None:
        import json

        ui = json.loads((REPO / "packaging" / "fpk" / "app" / "ui" / "config").read_text())
        embedded = ui[".url"]["hermes-desktop.Embedded"]
        conf = (REPO / "packaging" / "fpk" / "app" / "runtime" / "gateway" / "gateway.conf").read_text()
        values = dict(
            line.split("=", 1) for line in conf.splitlines() if "=" in line and not line.startswith("#")
        )
        self.assertEqual(embedded["gatewayPrefix"], values["prefix"])
        self.assertEqual(embedded["gatewaySocket"], values["socket_name"])
        self.assertEqual(embedded["url"], values["prefix"] + "/")
        self.assertNotIn("protocol", embedded)
        self.assertNotIn("port", embedded)

    def test_adapter_redirects_bare_mount_path(self) -> None:
        """Reload at /app/<app> (no trailing slash) must not 404 relative assets."""
        adapter = (
            REPO / "packaging" / "fpk" / "app" / "runtime" / "gateway" / "gateway_adapter.py"
        ).read_text(encoding="utf-8")
        self.assertIn("path_only == prefix", adapter)
        self.assertIn("302 Found", adapter)
        self.assertIn("prefix + \"/\"", adapter)


if __name__ == "__main__":
    unittest.main()
