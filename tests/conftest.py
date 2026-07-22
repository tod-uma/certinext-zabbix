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
