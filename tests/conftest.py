"""Shared pytest configuration for the certinext-zabbix test suite."""

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Pin typer's terminal detection and rendering width before import.

    typer reads ``PY_COLORS``/``FORCE_COLOR``/``GITHUB_ACTIONS`` into
    ``rich_utils.FORCE_TERMINAL`` and ``TERMINAL_WIDTH`` into
    ``rich_utils.MAX_WIDTH`` at **import** time, so a per-test fixture set
    too late cannot affect either — this hook runs before test collection
    imports the CLI modules. GitLab CI sets ``PY_COLORS='1'`` (for readable
    pytest output), which without this pin makes typer force rich's ANSI
    styling into captured ``--help`` output, breaking the substring
    assertions in tests/test_zabbix_push_cli_mocked.py.
    ``_TYPER_FORCE_DISABLE_TERMINAL`` is typer's own documented override for
    this.

    Args:
        config: The pytest configuration object (unused).
    """
    os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
    os.environ["TERMINAL_WIDTH"] = "100"
    os.environ["COLUMNS"] = "100"
    os.environ["LINES"] = "50"


@pytest.fixture(autouse=True)
def _clear_systemd_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear INVOCATION_ID/JOURNAL_STREAM so every test starts from a known non-systemd state.

    ``certinext.cli_support.setup_logging()``'s ``LogMode.AUTO`` reads these
    systemd-set env vars to decide whether to drop ``timestamp``/``pid``
    from non-interactive output. GitHub Actions' ``ubuntu-latest`` runner
    launches its own Actions Runner process as a systemd service, which
    leaks ``INVOCATION_ID`` into every job — unlike GitLab CI's
    Docker-executor images or local Windows dev, where it's absent. Without
    this fixture, any test exercising non-interactive logging output
    silently depends on which CI provider ran it (this bit ``certinext``'s
    own test suite the same way — see its MR !106). A test that specifically
    wants to simulate a systemd-invoked run should call
    ``monkeypatch.setenv("INVOCATION_ID", ...)`` itself, after this fixture
    has already cleared the ambient value.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
    """
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
