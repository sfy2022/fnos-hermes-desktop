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

    def test_empty_password_hash_is_repaired(self) -> None:
        """An empty dashboard.basic_auth must not be treated as configured."""
        for name in ("install_callback", "main"):
            text = read(name)
            self.assertIn('password_hash:[[:space:]]*"?scrypt', text, name)
            self.assertIn("password_hash:", text, name)
            self.assertIn("已补全空的 dashboard.basic_auth 哈希", text, name)
        install = read("install_callback")
        self.assertIn('if [ -n "${pw_hash}" ]; then', install)
        main = read("main")
        self.assertIn("chmod 770 \"${DATA_DIR}\"", main)
        self.assertIn("chmod 770 \"${DATA_DIR}\"", install)

    def test_libs_script_relocated(self) -> None:
        install = read("install_callback")
        self.assertIn("runtime/lib/libs.sh", install)
        self.assertNotIn("cmd/libs.sh", install)

    def test_uninstall_delete_data_is_real(self) -> None:
        """Deleting data must not be undone by logging recreating TRIM_PKGVAR."""
        uninstall = read("uninstall_callback")
        self.assertIn("UNINSTALL_LOG", uninstall)
        self.assertNotIn('mkdir -p "${LOG_DIR}"', uninstall)
        self.assertIn("cd / 2>/dev/null", uninstall)
        self.assertIn("WARN 数据目录仍然存在", uninstall)
        self.assertIn("true|1|yes|on", uninstall)

    def test_single_embedded_entry(self) -> None:
        import json

        ui = json.loads((REPO / "packaging" / "fpk" / "app" / "ui" / "config").read_text())
        entries = ui[".url"]
        self.assertEqual(list(entries), ["hermes-desktop.Embedded"])
        self.assertEqual(entries["hermes-desktop.Embedded"]["title"], "Hermes")
        self.assertNotIn("Application", entries)

    def test_install_wizard_configures_model(self) -> None:
        import json

        wizard = json.loads(
            (REPO / "packaging" / "fpk" / "wizard" / "install").read_text(encoding="utf-8")
        )
        fields = {
            item.get("field")
            for step in wizard
            for item in step.get("items", [])
            if item.get("field")
        }
        self.assertTrue(
            {"wizard_model_provider", "wizard_model_api_key", "wizard_model_name",
             "wizard_model_base_url"} <= fields
        )
        provider = next(
            item for step in wizard for item in step.get("items", [])
            if item.get("field") == "wizard_model_provider"
        )
        values = {opt["value"] for opt in provider["options"]}
        self.assertTrue({"openrouter", "anthropic", "deepseek", "custom", "skip"} <= values)

    def test_install_callback_applies_model_config(self) -> None:
        install = read("install_callback")
        self.assertIn("apply_model_config", install)
        self.assertIn("apply_model_config.py", install)
        self.assertIn("apply_model_config || true", install)

    def test_appshare_is_declared(self) -> None:
        import json

        resource = json.loads(
            (REPO / "packaging" / "fpk" / "config" / "resource").read_text(encoding="utf-8")
        )
        self.assertEqual(
            resource,
            {"data-share": {"shares": [{"name": "hermes-desktop"}]}},
        )
        for name in ("install_callback", "main"):
            text = read(name)
            self.assertIn("ensure_appshare", text, name)
            self.assertIn("@appshare", text, name)

    def test_build_only_on_upstream_release(self) -> None:
        workflows = REPO / ".github" / "workflows"
        build = (workflows / "build.yml").read_text(encoding="utf-8")
        sync = (workflows / "sync.yml").read_text(encoding="utf-8")
        # build.yml: manual/sync only; no daily schedule, no push trigger.
        self.assertNotIn("\n  schedule:", build)
        self.assertNotIn("\n  push:", build)
        self.assertIn("workflow_dispatch:", build)
        self.assertIn("needs.check.outputs.tag", build)
        self.assertIn(".upstream-release", build)
        # sync.yml: check the latest Release, not main commit.
        self.assertIn("releases/latest", sync)
        self.assertNotIn("refs/heads/main", sync)
        self.assertIn(".upstream-release", sync)
        self.assertTrue((REPO / ".upstream-release").is_file())


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
