"""Phase 3 contract tests — instruction-driven architecture.

Tests the new tools (TTS, music gen), pipeline manifests, style playbooks,
stage director skills, meta skills, and the animated-explainer pipeline.

NOTE (2026-09-05): Reconstructed from the committed ``.pyc`` cache after the
``.py`` source was lost between commits.  This is the **minimal viable** version
that restores pytest collection in ``tests/contracts/``.  Each class covers the
core contract only — full mock-heavy edge-case coverage from the original
(MagicMock-based execute paths, multimodal image flow, vertexai
auto-detect branching, base64 extraction variants) is not reconstructed and
should be added back incrementally.  Do NOT pretend this is equivalent
coverage to the lost source.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.pipeline_loader import (  # noqa: E402
    get_required_tools,
    get_stage_order,
    get_stage_review_focus,
    get_stage_skill,
    list_pipelines,
    load_pipeline,
)


# --------------------------------------------------------------------------- #
# google_credentials                                                         #
# --------------------------------------------------------------------------- #


class TestGoogleCredentials:
    def test_get_genai_client_with_google_api_key(self):
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "fake-key"}, clear=False):
            with patch("google.genai.Client") as mock_client:
                from tools.google_credentials import get_genai_client
                get_genai_client()
                mock_client.assert_called_once()


# --------------------------------------------------------------------------- #
# ElevenLabs TTS                                                             #
# --------------------------------------------------------------------------- #


class TestElevenLabsTTS:
    def test_identity(self):
        from tools.audio.elevenlabs_tts import ElevenLabsTTS
        info = ElevenLabsTTS().get_info()
        assert info["name"]
        assert info["capability"]

    def test_cost_estimate(self):
        from tools.audio.elevenlabs_tts import ElevenLabsTTS
        # estimate_cost takes a full inputs dict, charges per character.
        cost = ElevenLabsTTS().estimate_cost({"text": "x" * 1000})
        assert cost > 0

    def test_capabilities(self):
        from tools.audio.elevenlabs_tts import ElevenLabsTTS
        caps = ElevenLabsTTS.capabilities  # class-level attribute
        assert "text_to_speech" in caps


# --------------------------------------------------------------------------- #
# Piper TTS (local)                                                          #
# --------------------------------------------------------------------------- #


class TestPiperTTS:
    def test_identity(self):
        from tools.audio.piper_tts import PiperTTS
        info = PiperTTS().get_info()
        assert info["name"]

    def test_cost_is_free(self):
        from tools.audio.piper_tts import PiperTTS
        assert PiperTTS().estimate_cost({"text": "x" * 10000}) == 0

    def test_capabilities(self):
        from tools.audio.piper_tts import PiperTTS
        assert "text_to_speech" in PiperTTS.capabilities


# --------------------------------------------------------------------------- #
# Kokoro TTS (local, onnx)                                                   #
# --------------------------------------------------------------------------- #


class TestKokoroTTS:
    def test_identity(self):
        from tools.audio.kokoro_tts import KokoroTTS
        info = KokoroTTS().get_info()
        assert info["name"]

    def test_cost_is_free(self):
        from tools.audio.kokoro_tts import KokoroTTS
        assert KokoroTTS().estimate_cost({"text": "x" * 10000}) == 0


# --------------------------------------------------------------------------- #
# Transcriber (faster-whisper local)                                         #
# --------------------------------------------------------------------------- #


class TestTranscriber:
    def test_status_unavailable_when_package_missing(self):
        from tools.base_tool import ToolStatus
        from tools.analysis.transcriber import Transcriber
        with patch.dict(sys.modules, {"faster_whisper": None}):
            assert Transcriber().get_status() == ToolStatus.UNAVAILABLE


# --------------------------------------------------------------------------- #
# Google Cloud TTS                                                           #
# --------------------------------------------------------------------------- #


class TestGoogleTTS:
    def test_identity(self):
        from tools.audio.google_tts import GoogleTTS
        info = GoogleTTS().get_info()
        assert info["name"]


# --------------------------------------------------------------------------- #
# MusicGen (local / HF)                                                      #
# --------------------------------------------------------------------------- #


class TestMusicGen:
    def test_identity(self):
        from tools.audio.music_gen import MusicGen
        info = MusicGen().get_info()
        assert info["name"]


# --------------------------------------------------------------------------- #
# Google Lyria (music)                                                       #
# --------------------------------------------------------------------------- #


class TestGoogleMusic:
    def test_identity(self):
        from tools.audio.google_music import GoogleMusic
        info = GoogleMusic().get_info()
        assert info["name"]

    def test_duration_validation(self):
        from tools.audio.google_music import GoogleMusic
        gm = GoogleMusic()
        # Without GOOGLE_* creds, get_status is UNAVAILABLE and execute
        # short-circuits to a clean ToolResult(success=False, error=...). The
        # contract being tested is "no-credentials → graceful failure", not
        # the multimodal pipeline.
        with patch.object(gm, "_get_google_credentials_status", return_value=False):
            r = gm.execute({"prompt": "calm piano", "duration_seconds": 30})
            assert not r.success


# --------------------------------------------------------------------------- #
# Google Imagen                                                              #
# --------------------------------------------------------------------------- #


class TestGoogleImagen:
    def test_identity(self):
        from tools.graphics.google_imagen import GoogleImagen
        info = GoogleImagen().get_info()
        assert info["name"]


# --------------------------------------------------------------------------- #
# Veo video                                                                  #
# --------------------------------------------------------------------------- #


class TestVeoVideo:
    def test_identity(self):
        from tools.video.veo_video import VeoVideo
        info = VeoVideo().get_info()
        assert info["name"]

    def test_backend_auto_detect(self):
        from tools.video.veo_video import VeoVideo
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("GOOGLE_VERTEX_PROJECT", None)
            v = VeoVideo()
            # Without any GOOGLE_* creds the backend resolution must not raise;
            # status reporting gracefully falls back to UNAVAILABLE.
            from tools.base_tool import ToolStatus
            assert v.get_status() == ToolStatus.UNAVAILABLE


# --------------------------------------------------------------------------- #
# Tool registry                                                              #
# --------------------------------------------------------------------------- #


class TestNewToolsRegistry:
    def test_all_register(self):
        from tools.tool_registry import ToolRegistry
        reg = ToolRegistry()
        for mod_name, cls_name in [
            ("tools.audio.elevenlabs_tts", "ElevenLabsTTS"),
            ("tools.audio.piper_tts", "PiperTTS"),
            ("tools.audio.music_gen", "MusicGen"),
        ]:
            mod = __import__(mod_name, fromlist=[cls_name])
            reg.register(getattr(mod, cls_name)())
        # list_all returns tool NAMES (str), not objects.
        names = reg.list_all()
        assert "elevenlabs_tts" in names
        assert "piper_tts" in names
        assert "music_gen" in names


# --------------------------------------------------------------------------- #
# Capability metadata (cross-tool contract)                                  #
# --------------------------------------------------------------------------- #


class TestCapabilityMetadata:
    def test_tts_tools_expose_capability_provider_and_location(self):
        from tools.audio.elevenlabs_tts import ElevenLabsTTS
        info = ElevenLabsTTS().get_info()
        for key in ("capability", "provider", "tier", "name"):
            assert key in info, f"{key} missing from TTS tool info"


# --------------------------------------------------------------------------- #
# animated-explainer pipeline manifest                                        #
# --------------------------------------------------------------------------- #


class TestAnimatedExplainerManifest:
    def test_loads(self):
        manifest = load_pipeline("animated-explainer")
        assert manifest["name"] == "animated-explainer"

    def test_all_stages_present(self):
        manifest = load_pipeline("animated-explainer")
        stages = get_stage_order(manifest)
        assert len(stages) >= 5

    def test_every_stage_has_skill(self):
        manifest = load_pipeline("animated-explainer")
        for stage in get_stage_order(manifest):
            assert get_stage_skill(manifest, stage), f"stage {stage} has no skill"

    def test_listed(self):
        assert "animated-explainer" in list_pipelines()


# --------------------------------------------------------------------------- #
# video-template-remix pipeline manifest                                     #
# --------------------------------------------------------------------------- #


class TestVideoTemplateRemixManifest:
    def test_loads_with_complete_director_skills(self):
        manifest = load_pipeline("video-template-remix")
        stages = get_stage_order(manifest)
        # All 7 remix stages from cc59684: idea → script → scene_plan → assets → edit → compose → publish
        assert len(stages) == 7
        for stage in stages:
            skill = get_stage_skill(manifest, stage)
            assert skill, f"remix stage {stage} missing skill pointer"
            assert skill.startswith("pipelines/video-template-remix/"), (
                f"remix stage {stage} skill {skill!r} not in the video-template-remix skill tree"
            )

    def test_order_and_default_suggestion(self):
        manifest = load_pipeline("video-template-remix")
        order = get_stage_order(manifest)
        assert order[0] == "idea"
        assert order[-1] == "publish"


# --------------------------------------------------------------------------- #
# style playbooks                                                            #
# --------------------------------------------------------------------------- #


class TestStylePlaybooks:
    def test_all_listed(self):
        from styles.playbook_loader import list_playbooks
        books = list_playbooks()
        assert len(books) >= 1

    def test_loads_and_validates(self):
        from styles.playbook_loader import list_playbooks, load_playbook
        book_name = list_playbooks()[0]
        pb = load_playbook(book_name)
        # Playbook YAML wraps identity.name (not a top-level "name" key).
        assert pb["identity"]["name"]


# --------------------------------------------------------------------------- #
# Stage director skills + meta skills exist on disk                          #
# --------------------------------------------------------------------------- #


class TestSkillsExist:
    def test_director_skills_exist(self):
        skills_dir = Path(ROOT) / "skills"
        # animated-explainer pipeline points at pipelines/explainer/* (the
        # skill tree was renamed; pipeline_defs/animated-explainer.yaml:47+).
        required = [
            "pipelines/explainer/executive-producer.md",
            "pipelines/explainer/research-director.md",
            "pipelines/explainer/proposal-director.md",
            "pipelines/explainer/script-director.md",
            "pipelines/explainer/scene-director.md",
            "pipelines/explainer/asset-director.md",
            "pipelines/explainer/edit-director.md",
            "pipelines/explainer/compose-director.md",
            "pipelines/explainer/publish-director.md",
        ]
        for rel in required:
            assert (skills_dir / rel).exists(), f"missing skill: {rel}"


# --------------------------------------------------------------------------- #
# Remotion scaffold                                                          #
# --------------------------------------------------------------------------- #


class TestRemotionScaffold:
    def test_package_json_exists(self):
        remotion_dir = Path(ROOT) / "remotion-composer"  # actual dir uses a dash
        assert (remotion_dir / "package.json").exists()


# --------------------------------------------------------------------------- #
# VideoCompose operation surface                                              #
# --------------------------------------------------------------------------- #


class TestVideoComposeOperations:
    def test_render_operation_exists(self):
        from tools.video.video_compose import VideoCompose
        schema = VideoCompose.input_schema
        assert "properties" in schema
        assert "operation" in schema["properties"]
        assert "render" in schema["properties"]["operation"].get("enum", [])

    def test_render_rejects_missing_inputs(self):
        from tools.video.video_compose import VideoCompose
        # BaseTool.execute currently raises KeyError on missing required keys
        # (operation).  Contract being tested: a totally-empty input does not
        # return success=True and silently produce nothing — it surfaces the
        # missing-key error to the caller.
        try:
            result = VideoCompose().execute({})
        except KeyError:
            # Strict failure mode — acceptable: missing required input is rejected.
            return
        assert not result.success
