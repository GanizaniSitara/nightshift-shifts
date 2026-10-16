"""Shared test isolation: no test may depend on the machine's own Copilot MCP config or on the agent session
that happens to run the suite (goldens once captured CODEX_CI=1 from a Codex session)."""
import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_agent_environment(monkeypatch):
    # A fixed path that does not exist: no MCP config is read, and recorded goldens stay byte-stable.
    monkeypatch.setenv('COPILOT_HOME', 'C:\\nightshift-tests\\no-copilot-home')
    for name in list(os.environ):
        if name.upper().startswith('CODEX_'):
            monkeypatch.delenv(name, raising=False)
