from __future__ import annotations

import argparse

import pytest

from scripts.frameflow_remote_load_runner import e2e_command, e2e_environment, parse_stages


def test_parse_stages_requires_unique_ascending_range():
    assert parse_stages("1,2,4,5,6") == [1, 2, 4, 5, 6]
    with pytest.raises(argparse.ArgumentTypeError):
        parse_stages("2,1")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_stages("1,1")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_stages("1,13")


def test_e2e_command_never_contains_observer_token(monkeypatch):
    monkeypatch.setenv("FRAMEFLOW_OBSERVER_TOKEN", "super-secret-observer-token")
    args = argparse.Namespace(
        bff="http://renderer:8080",
        images=8,
        request_timeout_seconds=150,
        timeout_seconds=1800,
        poll_seconds=5,
        require_publish=False,
    )
    command = e2e_command(args, 4)
    rendered = " ".join(command)
    assert "super-secret-observer-token" not in rendered
    assert command[command.index("--jobs") + 1] == "4"
    assert "--remote-output" in command
    assert "FRAMEFLOW_OBSERVER_TOKEN" not in e2e_environment()
