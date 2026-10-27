"""Run one real ReviewPilot agent loop against Volcengine Ark.

Standalone launcher for the phase-one tool-use agent. It reads the Volcengine
key/api_base from ``.secrets.toml``, injects the key into the environment
(``LiteLLMAIHandler`` reads the key from the environment, not from the TOML),
builds a sample PR diff, and drives ``ReviewAgentRunner`` to completion.

Usage (from repo root):
    d:\\software\\study\\anaconda\\python.exe -m uv run python run_agent.py

Before running, set the model in ``pr_agent/settings/configuration.toml``:
    model="volcengine/<your-ark-endpoint-id>"   # e.g. volcengine/doubao-seed-1-6-250615
"""

import asyncio
import os

from pr_agent.agent_exec import ReviewAgentRunner
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings


def _inject_volcengine_key() -> None:
    """Copy the Volcengine key from .secrets.toml into the environment.

    ``LiteLLMAIHandler`` resolves the Volcengine key from ``VOLCENGINE_API_KEY``
    / ``ARK_API_KEY`` environment variables, so we mirror the secret here.
    """
    key = get_settings().get("volcengine", {}).get("key", "") or os.environ.get("VOLCENGINE_API_KEY", "")
    if not key:
        raise RuntimeError("Volcengine key not found. Fill [volcengine] key in pr_agent/settings/.secrets.toml")
    os.environ.setdefault("VOLCENGINE_API_KEY", key)
    os.environ.setdefault("ARK_API_KEY", key)
    # Optional explicit endpoint; LiteLLM already defaults to Ark's API v3.
    api_base = get_settings().get("volcengine", {}).get("api_base")
    if api_base:
        os.environ.setdefault("VOLCENGINE_API_BASE", api_base)


def _build_sample_diff() -> list[FilePatchInfo]:
    """Construct a tiny Python diff that should trigger the static tool."""
    patch = (
        "diff --git a/utils.py b/utils.py\n"
        "--- a/utils.py\n"
        "+++ b/utils.py\n"
        "@@ -1,6 +1,7 @@\n"
        " def process(data):\n"
        "     result = []\n"
        "     for item in data:\n"
        "         print(item)\n"
        "+        os.system(item)\n"
        "+    return result\n"
    )
    return [
        FilePatchInfo(
            base_file="utils.py",
            head_file="utils.py",
            patch=patch,
            filename="utils.py",
            edit_type=EDIT_TYPE.MODIFIED,
            num_plus_lines=2,
            num_minus_lines=0,
            language="Python",
        )
    ]


async def _main() -> None:
    _inject_volcengine_key()
    model = get_settings().get("config", {}).get("model")
    print(f"Using model: {model}")
    if model == "gpt-5.6" or not model.startswith("volcengine/"):
        print("WARNING: config.model is not a 'volcengine/...' model yet.")
        print("Set it in pr_agent/settings/configuration.toml before a real run.")

    diff_files = _build_sample_diff()
    runner = ReviewAgentRunner()

    state = await runner.run(diff_files)

    print("\n===== FINAL REVIEW =====")
    print(state.final_answer)
    print("\n===== TRAJECTORY =====")
    for i, msg in enumerate(state.messages):
        role = msg.get("role", "?")
        content = str(msg.get("content", ""))
        print(f"[{i}] {role}: {content[:400]}")

    print("\n===== STATS =====")
    print(f"steps: {state.step}")
    print(f"tool results: {len(state.tool_results)}")
    print(f"findings: {len(state.findings)}")


if __name__ == "__main__":
    asyncio.run(_main())
