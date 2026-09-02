"""Phase D feature-flag: namespace_version + canary bucket."""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def ns_module(monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_NAMESPACE_VERSION", raising=False)
    import lib.namespace_version as mod
    importlib.reload(mod)
    yield mod


def test_default_is_legacy(ns_module) -> None:
    """Unset env -> legacy."""
    assert ns_module.current_namespace_version() is ns_module.NamespaceVersion.LEGACY


def test_explicit_v2_only(ns_module, monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_NAMESPACE_VERSION", "v2-only")
    importlib.reload(ns_module)
    assert ns_module.current_namespace_version() is ns_module.NamespaceVersion.V2_ONLY


def test_explicit_canary(ns_module, monkeypatch) -> None:
    monkeypatch.setenv("OPENMONTAGE_NAMESPACE_VERSION", "canary")
    importlib.reload(ns_module)
    assert ns_module.current_namespace_version() is ns_module.NamespaceVersion.CANARY


def test_unknown_value_falls_back_to_legacy(ns_module, monkeypatch, caplog) -> None:
    monkeypatch.setenv("OPENMONTAGE_NAMESPACE_VERSION", "v2only")  # missing dash
    importlib.reload(ns_module)
    with caplog.at_level("WARNING"):
        v = ns_module.current_namespace_version()
    assert v is ns_module.NamespaceVersion.LEGACY
    assert any("not one of" in rec.message for rec in caplog.records)


def test_canary_bucket_stable_across_calls(ns_module) -> None:
    """Same (principal, project) -> same bucket."""
    a1 = ns_module.canary_bucket("user_42", "project_alpha")
    a2 = ns_module.canary_bucket("user_42", "project_alpha")
    assert a1 == a2


def test_canary_bucket_changes_with_inputs(ns_module) -> None:
    """Different inputs -> ~10% bucket coverage, deterministic."""
    in_bucket = sum(
        1 for i in range(1000)
        if ns_module.canary_bucket(f"user_{i}", f"proj_{i % 7}")
    )
    assert 50 <= in_bucket <= 200


def test_canary_bucket_rejects_empty_inputs(ns_module) -> None:
    assert ns_module.canary_bucket("", "p") is False
    assert ns_module.canary_bucket("u", "") is False
    assert ns_module.canary_bucket(None, "p") is False  # type: ignore[arg-type]


def test_resolve_workspace_layout_v2_only(ns_module, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENMONTAGE_NAMESPACE_VERSION", "v2-only")
    importlib.reload(ns_module)
    layout = ns_module.resolve_workspace_layout(
        principal_id="user_42",
        project_id="proj1",
        projects_root=tmp_path,
        namespace_key="d60195bef1e93dd103c9ce625738400d",
    )
    assert layout.mode is ns_module.NamespaceVersion.V2_ONLY
    assert layout.candidates == (tmp_path / "users" / "d60195bef1e93dd103c9ce625738400d" / "proj1",)
    assert layout.fallback is None


def test_resolve_workspace_layout_legacy_prefers_v2(ns_module, tmp_path: Path) -> None:
    layout = ns_module.resolve_workspace_layout(
        principal_id="raw_openid_xyz",
        project_id="proj1",
        projects_root=tmp_path,
        namespace_key="d60195bef1e93dd103c9ce625738400d",
    )
    assert layout.mode is ns_module.NamespaceVersion.LEGACY
    assert layout.preferred == tmp_path / "users" / "d60195bef1e93dd103c9ce625738400d" / "proj1"
    assert layout.fallback == tmp_path / "users" / "raw_openid_xyz" / "proj1"


def test_existing_root_returns_first_present(ns_module, tmp_path: Path) -> None:
    layout = ns_module.resolve_workspace_layout(
        principal_id="raw_openid_xyz",
        project_id="proj1",
        projects_root=tmp_path,
        namespace_key="d60195bef1e93dd103c9ce625738400d",
    )
    (tmp_path / "users" / "d60195bef1e93dd103c9ce625738400d" / "proj1").mkdir(parents=True)
    assert layout.existing_root() == tmp_path / "users" / "d60195bef1e93dd103c9ce625738400d" / "proj1"