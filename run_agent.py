"""Run one real ReviewPilot agent loop against the configured model.

Standalone launcher for the tool-use review agent. It reads the model from
``config.model`` and provider credentials from ``.secrets.toml`` (the shared
``LiteLLMAIHandler`` resolves them), builds a sample PR diff, and drives
``ReviewAgentRunner`` to completion.

Usage (from repo root):
    uv run python run_agent.py

Before running, set the model and provider key in ``pr_agent/settings/.secrets.toml``:
    [config]
    model = "deepseek/deepseek-chat"      # any LiteLLM-supported model
    [deepseek]
    key = "sk-..."
"""

import asyncio

from pr_agent.agent_exec import ReviewAgentRunner
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings


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
    model = get_settings().get("config", {}).get("model")
    print(f"Using model: {model}")

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
