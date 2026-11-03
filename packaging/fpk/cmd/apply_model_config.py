#!/usr/bin/env python3
"""Apply model settings from the fnOS install wizard to Hermes config.

Usage:
    HERMES_MODEL_PROVIDER=openrouter \
    HERMES_MODEL_API_KEY=... \
    HERMES_MODEL_NAME=... \
    HERMES_MODEL_BASE_URL=... \
    python3 apply_model_config.py <config.yaml> <data-dir/.env>

The installer runs before Hermes' venv exists, so this uses only stdlib.
API keys are written to config.yaml only for explicit custom endpoints
(Hermes ignores model.api_key for built-in providers) and to the
HERMES_HOME/.env file otherwise. Both files are chmod 0600.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PROVIDERS = {
    "openrouter": {
        "provider": "openrouter",
        "env": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4.5",
        "base_url": "https://openrouter.ai/api/v1",
        "needs_key": True,
        "api_key_in_config": False,
    },
    "anthropic": {
        "provider": "anthropic",
        "env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-5",
        "base_url": "",
        "needs_key": True,
        "api_key_in_config": False,
    },
    "deepseek": {
        "provider": "custom",
        "env": "",
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "needs_key": True,
        "api_key_in_config": True,
    },
    "custom": {
        "provider": "custom",
        "env": "",
        "default_model": "",
        "base_url": "",
        "needs_key": False,
        "api_key_in_config": True,
    },
}

MODEL_HEADER = "# ── 模型配置（安装向导生成，可稍后在 Hermes 设置中修改） ──"


def yaml_string(value: str) -> str:
    """JSON strings are valid YAML double-quoted scalars and escape safely."""
    return json.dumps(value, ensure_ascii=False)


def set_env_value(path: Path, name: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    replaced = False
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.match(rf"^\s*{re.escape(name)}\s*=", line):
                if not replaced:
                    lines.append(f"{name}={value}")
                    replaced = True
                continue
            lines.append(line)
    if not replaced:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: apply_model_config.py <config.yaml> <data-dir/.env>", file=sys.stderr)
        return 2

    config_path = Path(argv[1])
    env_path = Path(argv[2])

    provider_key = os.environ.get("HERMES_MODEL_PROVIDER", "skip").strip().lower()
    if provider_key in ("", "skip", "none"):
        print("model: skipped by wizard")
        return 0

    spec = PROVIDERS.get(provider_key)
    if spec is None:
        print(f"model: unknown provider {provider_key!r}", file=sys.stderr)
        return 2

    api_key = os.environ.get("HERMES_MODEL_API_KEY", "").strip()
    model_name = os.environ.get("HERMES_MODEL_NAME", "").strip() or spec["default_model"]
    base_url = os.environ.get("HERMES_MODEL_BASE_URL", "").strip() or spec["base_url"]

    if spec["needs_key"] and not api_key:
        print("model: API key is empty; configuration skipped", file=sys.stderr)
        return 2
    if spec["provider"] == "custom" and not base_url:
        print("model: custom provider requires a base URL", file=sys.stderr)
        return 2
    if spec["provider"] == "custom" and not model_name:
        print("model: custom provider requires a model name", file=sys.stderr)
        return 2
    if any(ch in api_key for ch in "\r\n"):
        print("model: API key contains a newline; refused", file=sys.stderr)
        return 2

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text("# Hermes Agent 配置\n", encoding="utf-8")
    text = config_path.read_text(encoding="utf-8")
    if re.search(r"(?m)^model\s*:", text):
        print("model: config already has a model section; left untouched")
        return 0

    lines = [
        "",
        MODEL_HEADER,
        "model:",
        f"  provider: {yaml_string(spec['provider'])}",
        f"  default: {yaml_string(model_name)}",
    ]
    if base_url:
        lines.append(f"  base_url: {yaml_string(base_url)}")
    if spec["api_key_in_config"] and api_key:
        lines.append(f"  api_key: {yaml_string(api_key)}")

    config_path.write_text(text.rstrip("\n") + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)

    if spec["env"] and api_key:
        set_env_value(env_path, spec["env"], api_key)

    print(f"model: provider={spec['provider']} name={model_name} key={'yes' if api_key else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
