"""Thin wrapper over the existing AI handler used by the ReviewPilot agent.

Reuses ``LiteLLMAIHandler`` (the repository default) via the same factory
pattern as ``PRAgent``, and reads the model name from ``config.model`` rather
than introducing a second configuration system. Tests inject a ``MockHandler``
factory instead so no real API key or network is needed.
"""

from typing import Any

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.config_loader import get_settings


class AgentLLM:
    """Minimal async interface to the shared AI handler."""

    def __init__(self, ai_handler_factory=LiteLLMAIHandler) -> None:
        self._factory = ai_handler_factory
        self._handler = ai_handler_factory()

    def model_name(self) -> str:
        # Tolerate a partially-loaded settings object (e.g. in unit tests) by
        # falling back through .get(); production always has config.model set.
        config = get_settings().get("config", {}) or {}
        return config.get("model", "gpt-5.6")

    async def complete(self, system: str, user: str, temperature: float = 0.2) -> tuple[str, Any]:
        """Return ``(content, finish_reason)`` from the underlying handler."""
        resp, finish_reason = await self._handler.chat_completion(
            model=self.model_name(),
            system=system,
            user=user,
            temperature=temperature,
        )
        return resp, finish_reason
