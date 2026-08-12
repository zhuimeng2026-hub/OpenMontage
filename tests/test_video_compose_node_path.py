"""Tests for Remotion availability node-on-PATH fallback discovery."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose

IS_WIN = sys.platform == "win32"


def _make_fake_nvm(home: Path, version: str = "v22.22.1") -> Path:
    bin_dir = home / ".nvm" / "versions" / "node" / version / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "npx").write_text("#!/bin/sh\n")
    (bin_dir / "npx").chmod(0o755)
    (bin_dir / "node").write_text("#!/bin/sh\n")
    (bin_dir / "node").chmod(0o755)
    return bin_dir


def _fake_home(monkeypatch, home: Path):
    # 平台无关地把 ~ 指向 fakehome（Windows 读 USERPROFILE 而非 HOME）
    monkeypatch.setattr(os.path, "expanduser", lambda x: str(home))
    monkeypatch.delenv("NVM_DIR", raising=False)


def test_ensure_node_on_path_injects_nvm_when_missing(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "fakehome"
        home.mkdir()
        bin_dir = _make_fake_nvm(home)

        _fake_home(monkeypatch, home)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        if not IS_WIN:
            assert shutil.which("npx") is None

        vc = VideoCompose()
        vc._ensure_node_on_path()

        assert str(bin_dir) in os.environ["PATH"]
        if not IS_WIN:
            assert shutil.which("npx") is not None


def test_ensure_node_on_path_picks_highest_version(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "fakehome"
        home.mkdir()
        _make_fake_nvm(home, "v20.0.0")
        high = _make_fake_nvm(home, "v22.22.1")

        _fake_home(monkeypatch, home)
        monkeypatch.setenv("PATH", "/usr/bin")

        vc = VideoCompose()
        vc._ensure_node_on_path()
        assert str(high) in os.environ["PATH"]
        if not IS_WIN:
            assert shutil.which("npx") is not None


def test_ensure_node_on_path_noop_when_npx_present(monkeypatch):
    vc = VideoCompose()
    before = os.environ.get("PATH", "")
    vc._ensure_node_on_path()
    assert os.environ.get("PATH", "") == before or shutil.which("npx") is not None
