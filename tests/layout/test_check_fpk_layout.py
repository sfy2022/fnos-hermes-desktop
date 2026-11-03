#!/usr/bin/env python3
"""Layout checker tests: one good package + the six bad cases required by T6.

Every test builds its own package in a temporary directory from
``build_good_package`` (stdlib only, no committed binaries), mutates one
aspect, runs the real CLI and asserts exit code + rule id.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "check_fpk_layout.py"

MANIFEST = """\
appname="hermes-desktop"
version="0.22.13"
display_name="Hermes Desktop"
desc="layout checker fixture"
source="thirdparty"
platform="x86"
maintainer="NousResearch"
maintainer_url="https://github.com/NousResearch/hermes-agent"
distributor="tester"
distributor_url="https://example.invalid/repo"
desktop_uidir="ui"
desktop_applaunchname="hermes-desktop.Embedded"
service_port="9120"
checkport="false"
ctl_stop="true"
install_dep_apps="python312"
"""

ENTRY_CONFIG = {
    ".url": {
        "hermes-desktop.Embedded": {
            "title": "Hermes",
            "icon": "images/icon_{0}.png",
            "type": "iframe",
            "gatewayPrefix": "/app/hermes-desktop",
            "gatewaySocket": "app.sock",
            "url": "/app/hermes-desktop/",
            "allUsers": True,
        },
    }
}

PRIVILEGE = {
    "defaults": {"run-as": "package"},
    "username": "hermes-desktop",
    "groupname": "hermes-desktop",
}

WIZARD_ITEMS = [
    {
        "stepTitle": "Setup",
        "items": [
            {"type": "tips", "helpText": "fixture"},
            {"type": "text", "field": "wizard_web_user", "label": "user"},
            {"type": "password", "field": "wizard_web_pass", "label": "pass",
             "initValue": ""},
        ],
    }
]

LIFECYCLE = (
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


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def write_png(path: Path, width: int, height: int,
              color=(30, 80, 200)) -> None:
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_elf_x86_64(path: Path) -> None:
    """Minimal valid-enough ELF64 x86_64 header for the arch scanner."""
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # little endian
    header[6] = 1  # version
    struct.pack_into("<H", header, 16, 2)  # e_type ET_EXEC
    struct.pack_into("<H", header, 18, 62)  # EM_X86_64
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + b"\x00" * 64)


def build_good_package(root: Path) -> Path:
    write_text(root / "manifest", MANIFEST)
    write_png(root / "ICON.PNG", 64, 64)
    write_png(root / "ICON_256.PNG", 256, 256)

    write_text(root / "config" / "privilege",
               json.dumps(PRIVILEGE, ensure_ascii=False, indent=2) + "\n")
    write_text(root / "config" / "resource", "{}\n")

    write_text(root / "app" / "ui" / "config",
               json.dumps(ENTRY_CONFIG, ensure_ascii=False, indent=2) + "\n")
    write_png(root / "app" / "ui" / "images" / "icon_64.png", 64, 64)
    write_png(root / "app" / "ui" / "images" / "icon_256.png", 256, 256)
    write_png(root / "app" / "ui" / "images" / "icon_512.png", 512, 512)
    write_text(root / "app" / "webapp" / "index.html",
               "<!doctype html><title>fixture</title>\n")

    write_text(
        root / "app" / "runtime" / "gateway" / "gateway_adapter.py",
        "#!/usr/bin/env python3\n\"\"\"fixture adapter\"\"\"\n",
    )
    write_text(
        root / "app" / "runtime" / "gateway" / "gateway.conf",
        "prefix=/app/hermes-desktop\nsocket_name=app.sock\n",
    )
    write_text(
        root / "app" / "runtime" / "lib" / "libs.sh",
        "#!/bin/bash\n# fixture\n",
    )
    write_text(root / "app" / "plugins" / ".keep", "")
    write_text(root / "app" / "skills" / ".keep", "")

    for name in LIFECYCLE:
        if name == "main":
            script = (
                "#!/bin/bash\n"
                "case \"$1\" in\n"
                "  start) exit 0 ;;\n"
                "  stop) exit 0 ;;\n"
                "  status) exit 3 ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
        else:
            script = "#!/bin/bash\nexit 0\n"
        write_text(root / "cmd" / name, script)
    write_text(root / "cmd" / "pause_gateway.py",
               "#!/usr/bin/env python3\n\"\"\"fixture\"\"\"\n")

    for name in ("install", "config", "upgrade", "uninstall"):
        write_text(
            root / "wizard" / name,
            json.dumps(WIZARD_ITEMS, ensure_ascii=False, indent=2) + "\n",
        )
    return root


def run_checker(target: Path, *extra: str):
    completed = subprocess.run(
        [sys.executable, str(CHECKER), str(target), "--json", *extra],
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


class LayoutCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        # ``tempfile.mkdtemp`` creates 0700 dirs which some sandboxes make
        # unwritable; a plain unique directory under TMP works everywhere.
        self.tmp = Path(tempfile.gettempdir()) / (
            "fpk-layout-" + uuid.uuid4().hex
        )
        self.tmp.mkdir()
        self.good = build_good_package(self.tmp / "good")

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
        self.assertEqual(payload["summary"]["exit_code"], 1)
        rules = [item["rule"] for item in payload["violations"]]
        self.assertIn(rule, rules, payload["violations"])
        return payload

    # ------------------------------------------------------------ good case
    def test_good_package_passes(self) -> None:
        completed, payload = run_checker(self.good)
        self.assertIsNotNone(payload, completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(payload["summary"]["violations"], 0)
        self.assertFalse(payload["template_mode"])

    def test_json_output_shape(self) -> None:
        completed, payload = run_checker(self.good)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("violations", payload)
        self.assertIn("warnings", payload)
        self.assertIn("summary", payload)
        self.assertIn("package_dir", payload)

    # --------------------------------------------- the six required bad cases
    def test_missing_manifest(self) -> None:
        target = self.clone("missing-manifest")
        (target / "manifest").unlink()
        self.assert_violation(target, "PKG-MANIFEST-01")

    def test_platform_all_with_binary(self) -> None:
        target = self.clone("platform-all-binary")
        manifest = (target / "manifest").read_text(encoding="utf-8")
        manifest = manifest.replace('platform="x86"', 'platform="all"')
        write_text(target / "manifest", manifest)
        write_elf_x86_64(target / "app" / "skills" / "bin" / "native-tool")
        self.assert_violation(target, "PKG-ARCH-01")

    def test_invalid_json(self) -> None:
        target = self.clone("invalid-json")
        write_text(target / "config" / "privilege",
                   '{ "defaults": { "run-as": "package" }\n')
        self.assert_violation(target, "PKG-PRIV-01")

    def test_extra_root_file(self) -> None:
        target = self.clone("extra-root-file")
        write_text(target / "README.md", "# not allowed in the package\n")
        self.assert_violation(target, "PKG-ROOT-02")

    def test_crlf_line_endings(self) -> None:
        target = self.clone("crlf")
        main = target / "cmd" / "main"
        data = main.read_bytes().replace(b"\r\n", b"\n")
        main.write_bytes(data.replace(b"\n", b"\r\n"))
        self.assert_violation(target, "PKG-ENC-02")

    def test_pycache_junk(self) -> None:
        target = self.clone("pycache")
        write_text(target / "app" / "__pycache__" / "x.pyc", "junk")
        self.assert_violation(target, "PKG-JUNK-01")

    def test_plugin_dashboard_dist_is_allowed(self) -> None:
        """Plugin frontend source lives in dashboard/dist and must pass."""
        target = self.clone("plugin-dist")
        write_text(
            target / "app" / "plugins" / "demo-plugin" / "dashboard" / "dist"
            / "index.js",
            "/* hand-written plugin bundle */\n",
        )
        write_text(
            target / "app" / "plugins" / "demo-plugin" / "dashboard" / "dist"
            / "style.css",
            "/* hand-written plugin style */\n",
        )
        completed, payload = run_checker(target)
        self.assertIsNotNone(payload, completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        junk = [item for item in payload["violations"]
                if item["rule"] == "PKG-JUNK-01"]
        self.assertEqual(junk, [])

    def test_other_dist_directory_still_junk(self) -> None:
        target = self.clone("other-dist")
        write_text(target / "app" / "dist" / "bundle.js", "/* build output */\n")
        self.assert_violation(target, "PKG-JUNK-01")

    # -------------------------------------------------------- template mode
    def test_template_mode_downgrades_webapp_and_icons(self) -> None:
        target = self.clone("template")
        for relative in (
            "app/webapp",
            "ICON.PNG",
            "ICON_256.PNG",
            "app/ui/images/icon_64.png",
            "app/ui/images/icon_256.png",
        ):
            path = target / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        completed, payload = run_checker(target, "--template")
        self.assertIsNotNone(payload, completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertTrue(payload["template_mode"])
        warning_rules = {item["rule"] for item in payload["warnings"]}
        self.assertIn("PKG-APP-04", warning_rules)
        self.assertIn("PKG-ICON-01", warning_rules)

    def test_source_tree_auto_detected_as_template(self) -> None:
        target = self.clone("auto-template")
        shutil.rmtree(target / "app" / "webapp")
        completed, payload = run_checker(target)
        self.assertIsNotNone(payload, completed.stderr)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertTrue(payload["template_mode"])
        rules = {item["rule"] for item in payload["warnings"]}
        self.assertIn("PKG-MODE-01", rules)

    # ------------------------------------------------------------- exit code 2
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
