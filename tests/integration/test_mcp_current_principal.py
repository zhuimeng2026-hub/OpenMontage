"""Auth-source precedence tests for ``mcp_server.current_principal``."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("MCP_TEST_SKIP_VOICEBOX_FIXTURES", "1")
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import pytest

import mcp_server
import lib.principal_registry as pr
from lib.mcp_session import reset_mcp_session_id, set_mcp_session_id
from lib.principal_registry import Principal, PrincipalNotFound


@pytest.fixture
def registry_db(tmp_path: Path):
    path = tmp_path / "principals.db"
    pr._reset_for_tests()
    pr.configure(path)
    yield path
    pr._reset_for_tests()


def test_existing_session_uses_registry_namespace_after_rotation(
    registry_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "principal-old")
    monkeypatch.delenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", raising=False)
    pr._secret_value = None
    stored = Principal(kind="user", principal_id="alice")
    pr.bind("session-alice", stored)

    # Simulate a restart/rotation. The ContextVar intentionally names another
    # principal; an existing session must not use it as an identity source.
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "principal-new")
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", "principal-old")
    pr._secret_value = None
    user_token = mcp_server._user_id_ctx.set("mallory")
    session_token = set_mcp_session_id("session-alice")
    try:
        got = mcp_server.current_principal()
    finally:
        reset_mcp_session_id(session_token)
        mcp_server._user_id_ctx.reset(user_token)

    assert got.principal_id == "alice"
    assert got.namespace_key == stored.namespace_key


def test_bound_session_does_not_fallback_to_fast_path(
    registry_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_token = mcp_server._user_id_ctx.set("fast-path-user")
    session_token = set_mcp_session_id("missing-session")
    try:
        with pytest.raises(PrincipalNotFound):
            mcp_server.current_principal()
    finally:
        reset_mcp_session_id(session_token)
        mcp_server._user_id_ctx.reset(user_token)


def test_initialize_without_session_can_use_fast_path(
    registry_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "principal-current")
    pr._secret_value = None
    user_token = mcp_server._user_id_ctx.set("initialize-user")
    initialize_token = mcp_server._initialize_request_ctx.set(True)
    try:
        got = mcp_server.current_principal()
    finally:
        mcp_server._initialize_request_ctx.reset(initialize_token)
        mcp_server._user_id_ctx.reset(user_token)
    assert got.principal_id == "initialize-user"


def test_tool_call_without_session_cannot_use_fast_path(
    registry_db: Path,
) -> None:
    user_token = mcp_server._user_id_ctx.set("tool-user")
    try:
        with pytest.raises(PrincipalNotFound):
            mcp_server.current_principal()
    finally:
        mcp_server._user_id_ctx.reset(user_token)
