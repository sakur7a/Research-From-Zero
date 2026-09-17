"""Test the model endpoint configured in the environment, without ever printing a key.

    RE0_ENV_FILE=~/.codex/skills/.env python scripts/model_probe.py

Reads `RE0_ENV_FILE` first if set, then `RE0_LLM_BASE_URL` / `RE0_LLM_MODEL` /
`RE0_LLM_API_KEY` (plus optional `RE0_LLM_TOKEN_PARAMETER`). Those are the same variables
`run.py` builds its in-memory configuration from, so a pass here means the real agent can
use the same endpoint.

Only names are reported, never values: a key must not reach a terminal, log, task record or
export. The endpoint URL is printed because `validate_endpoint` already refuses a URL that
carries credentials or a query string, so it cannot contain the key.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from re0.agent.model import ChatModel, ModelError, ModelVault  # noqa: E402
from re0.env_file import load                                  # noqa: E402

VARIABLES = ("RE0_LLM_BASE_URL", "RE0_LLM_MODEL", "RE0_LLM_API_KEY", "RE0_LLM_TOKEN_PARAMETER")


def main() -> int:
    env_file = os.getenv("RE0_ENV_FILE", "").strip()
    if env_file:
        applied = load(env_file)
        print(f"env file: {env_file} ({len(applied)} variables set)")
    else:
        print("env file: none (RE0_ENV_FILE is unset); using the ambient environment only")
    for name in VARIABLES:
        # Presence only. A value must never be printed, not even a masked prefix.
        print(f"  {name}: {'set' if os.getenv(name) else 'not set'}")

    vault = ModelVault()
    if vault.startup_error:
        print(f"configuration rejected: {vault.startup_error}")
        return 2
    if not vault.public().get("configured"):
        print("no usable model configuration: RE0_LLM_BASE_URL and RE0_LLM_MODEL are required, "
              "and a hosted endpoint also needs RE0_LLM_API_KEY")
        return 2

    config = vault.snapshot()
    print(f"endpoint: {config.base_url} | model: {config.model} | output parameter: {config.token_parameter}")
    try:
        result = ChatModel(config).test()
    except ModelError as exc:
        # Application-authored message; provider bodies are not relayed.
        print(f"tool-call probe failed: {exc}")
        return 2
    print(f"tool-call probe passed — {result['note']}")
    print("provider-reported usage:", result.get("usage"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
