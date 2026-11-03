"""Tests for the install-wizard model configuration helper.

Runs ``packaging/fpk/cmd/apply_model_config.py`` as a subprocess (stdlib only)
against temporary config/.env files, so no Hermes install is needed.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "packaging" / "fpk" / "cmd" / "apply_model_config.py"


class ModelConfigTests(unittest.TestCase):
    def run_helper(self, provider: str, key: str = "", name: str = "", base: str = ""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = root / "config.yaml"
        cfg.write_text("# Hermes Agent 配置\n", encoding="utf-8")
        env = {
            **os.environ,
            "HERMES_MODEL_PROVIDER": provider,
            "HERMES_MODEL_API_KEY": key,
            "HERMES_MODEL_NAME": name,
            "HERMES_MODEL_BASE_URL": base,
        }
        proc = subprocess.run(
            [sys.executable, str(HELPER), str(cfg), str(root / ".env")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return proc, cfg, root / ".env"

    def test_openrouter_uses_env_not_config(self) -> None:
        proc, cfg, envf = self.run_helper("openrouter", key="sk-or-test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn("model:", text)
        self.assertIn('provider: "openrouter"', text)
        self.assertIn('default: "anthropic/claude-sonnet-4.5"', text)
        self.assertNotIn("sk-or-test", text)
        self.assertIn("OPENROUTER_API_KEY=sk-or-test", envf.read_text(encoding="utf-8"))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)

    def test_anthropic_default_model(self) -> None:
        proc, cfg, envf = self.run_helper("anthropic", key="sk-ant-test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn('provider: "anthropic"', text)
        self.assertIn('default: "claude-sonnet-4-5"', text)
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-test", envf.read_text(encoding="utf-8"))

    def test_custom_writes_base_key_and_model(self) -> None:
        proc, cfg, _envf = self.run_helper(
            "custom", key="custom-key", name="my-model", base="https://llm.internal/v1"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn('provider: "custom"', text)
        self.assertIn('default: "my-model"', text)
        self.assertIn('base_url: "https://llm.internal/v1"', text)
        self.assertIn('api_key: "custom-key"', text)

    def test_deepseek_defaults(self) -> None:
        proc, cfg, _envf = self.run_helper("deepseek", key="sk-ds")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = cfg.read_text(encoding="utf-8")
        self.assertIn('provider: "custom"', text)
        self.assertIn('default: "deepseek-chat"', text)
        self.assertIn('base_url: "https://api.deepseek.com/v1"', text)

    def test_skip_and_empty_key_do_not_block(self) -> None:
        proc, cfg, envf = self.run_helper("skip", key="ignored")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("model:", cfg.read_text(encoding="utf-8"))
        self.assertFalse(envf.exists())

        proc2, cfg2, _ = self.run_helper("openrouter", key="")
        self.assertNotEqual(proc2.returncode, 0)
        self.assertNotIn("model:", cfg2.read_text(encoding="utf-8"))

    def test_existing_model_section_untouched(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "config.yaml"
        cfg.write_text("model:\n  provider: openai\n", encoding="utf-8")
        env = {**os.environ, "HERMES_MODEL_PROVIDER": "openrouter", "HERMES_MODEL_API_KEY": "x"}
        proc = subprocess.run(
            [sys.executable, str(HELPER), str(cfg), str(Path(tmp.name) / ".env")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(cfg.read_text(encoding="utf-8"), "model:\n  provider: openai\n")


if __name__ == "__main__":
    unittest.main()
