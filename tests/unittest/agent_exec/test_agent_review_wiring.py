"""Tests for the ReviewPilot agent loop wiring into PRReviewer.run().

The agent loop is opt-in via ``[agent_exec].enabled``. When disabled (the
default) the single-shot review pipeline runs unchanged; when enabled the
bounded agent runs over the PR diff files and publishes its findings.
"""

import pytest

from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _FakeState:
    final_answer = "AGENT: high-risk finding in a.py"


class _FakeRunner:
    """Stand-in for ReviewAgentRunner that records the diff files it received."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.diff_files = None
        _FakeRunner.instances.append(self)

    async def run(self, diff_files):
        self.diff_files = diff_files
        return _FakeState()


class _StubGitProvider:
    def __init__(self):
        self.published = []
        self.diff_files = [object()]

    def get_diff_files(self):
        return self.diff_files

    def publish_comment(self, text, **kwargs):
        self.published.append(text)


def _make_reviewer():
    """Build a PRReviewer without invoking its heavy __init__."""
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = _StubGitProvider()
    reviewer.prediction = None
    return reviewer


@pytest.mark.asyncio
async def test_agent_wiring_disabled_by_default_is_noop(monkeypatch):
    # When disabled, the wiring must not run the agent or publish anything.
    monkeypatch.setattr("pr_agent.agent_exec.runner.is_enabled", lambda: False)
    _FakeRunner.instances.clear()

    reviewer = _make_reviewer()
    result = await reviewer._run_agent_review()

    assert result is False
    assert _FakeRunner.instances == []
    assert reviewer.git_provider.published == []
    assert reviewer.prediction is None


@pytest.mark.asyncio
async def test_agent_wiring_runs_and_publishes_when_enabled(monkeypatch):
    monkeypatch.setattr("pr_agent.agent_exec.runner.is_enabled", lambda: True)
    monkeypatch.setattr("pr_agent.agent_exec.runner.ReviewAgentRunner", _FakeRunner)
    _FakeRunner.instances.clear()

    snap = snapshot_settings(["config.publish_output"])
    try:
        get_settings().set("config.publish_output", True)
        reviewer = _make_reviewer()

        result = await reviewer._run_agent_review()

        assert result is True
        # The agent ran over the diff files and published its final answer.
        assert len(_FakeRunner.instances) == 1
        assert _FakeRunner.instances[0].diff_files == reviewer.git_provider.get_diff_files()
        assert reviewer.prediction == _FakeState.final_answer
        assert reviewer.git_provider.published == [_FakeState.final_answer]
    finally:
        restore_settings(snap)
