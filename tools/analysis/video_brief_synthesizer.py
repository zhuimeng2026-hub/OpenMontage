"""Video brief synthesizer tool.

Fills the vision-/content-only fields that `video_analyzer` leaves blank
(per source comments like `description: ""  # Agent fills via vision`,
`key_elements_to_replicate: []  # Agent fills via analysis`).

The synthesizer is multimodal:

  video_analyzer skeleton  ─┐
  keyframe JPGs            ─┤
  transcript segments      ─┴──►  VLM (Anthropic-compatible endpoint)  ──►  populated research_brief.json

Provider is the project's Anthropic-compatible gateway (configured via
ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN env vars). The tool degrades
gracefully — if no VLM is reachable, it returns success=True with
``synthesis.status = "skipped"`` so the caller can choose to retry or
fall back to manual synthesis.

Per-user isolation mirrors the rest of the analysis tools: callers pass
``project_id`` (defaults to "references") and either rely on the MCP session
for the user-id or pass ``userid`` explicitly.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolTier,
    ToolRuntime,
)


# Fields the tool fills. Keep this in sync with the brief schema so that
# future additions to the skeleton get picked up automatically when the
# prompt template is updated.
SYNTHESIS_FIELDS = {
    "content_analysis.summary": "1-2 sentence plain-language summary of the reference video",
    "content_analysis.topics": "3-5 short topic phrases",
    "content_analysis.target_audience": "1-sentence audience description",
    "style_profile.color_palette.primary_colors": "3-6 primary colors observed (e.g. '#1a1a1a black jersey')",
    "style_profile.color_palette.accent_colors": "1-4 accent colors (e.g. '#ffd400 yellow subtitle')",
    "style_profile.color_palette.overall_mood": "1-3 word mood (e.g. 'warm natural-light')",
    "style_profile.typography_observed": "1-sentence font/style description of on-screen text",
    "style_profile.transition_types": "list of transition types observed (jump cut, fade, etc.)",
    "style_profile.music_style": "1-sentence BGM description (tempo, mood)",
    "style_profile.subtitle_style": "1-sentence subtitle style (position, color, highlight)",
    "style_profile.production_quality": "one of: 'professional' / 'prosumer' / 'amateur'",
    "style_profile.closest_playbook": "1-3 word style label, e.g. 'flat-motion-graphics' / 'lifestyle-vlog' / 'cinematic'",
    "style_profile.playbook_delta": "1-sentence honest gap between this video and the closest playbook",
    "style_profile.narration_style.delivery_style": "1-sentence delivery description (fast/slow, casual/professional)",
    "replication_guidance.key_elements_to_replicate": "3-5 form elements worth borrowing as-is",
    "replication_guidance.elements_requiring_custom_work": "3-5 content elements that MUST be replaced to avoid copying",
    "replication_guidance.creative_differentiation_seeds": "3-5 idea seeds for a remixed version",
}


class VideoBriefSynthesizer(BaseTool):
    name = "video_brief_synthesizer"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "analysis"
    provider = "anthropic-compatible"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:ANTHROPIC_BASE_URL", "env:ANTHROPIC_AUTH_TOKEN"]
    install_instructions = (
        "Set ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (and optionally "
        "ANTHROPIC_DEFAULT_SONNET_MODEL) in .env. The tool talks to any "
        "Anthropic-compatible /v1/messages endpoint, so it works with "
        "the project's aikey4k proxy and any direct Anthropic API key."
    )
    agent_skills = ["video-understand"]

    best_for = [
        "filling the agent-fillable vision fields in a video_analysis_brief.json",
        "producing a research_brief.json ready for downstream idea-director",
        "grounded (frame + transcript based) LLM analysis of a reference video",
    ]
    not_good_for = [
        "downloading the source video (use video_downloader first)",
        "transcribing audio (use transcriber first)",
        "scoring or ranking videos (no opinionated output)",
    ]

    input_schema = {
        "type": "object",
        "required": ["brief_path"],
        "properties": {
            "brief_path": {
                "type": "string",
                "description": "Path to a video_analysis_brief.json (the video_analyzer skeleton).",
            },
            "frames_dir": {
                "type": "string",
                "description": "OPTIONAL directory of keyframe JPGs. If omitted, the tool "
                               "looks at brief['keyframes'] and resolves each entry's "
                "'path' field. Frame sampling is throttled to <=16 frames.",
            },
            "transcript_path": {
                "type": "string",
                "description": "OPTIONAL path to a transcript file (.json with 'segments' or "
                               ".txt with the full text). If omitted, the tool looks for "
                               "_audio_transcript.json / transcript.txt next to the brief.",
            },
            "project_id": {
                "type": "string",
                "default": "references",
                "pattern": "^[a-zA-Z0-9._-]{1,64}$",
            },
            "userid": {
                "type": "string",
                "pattern": "^[a-zA-Z0-9_-]{1,64}$",
                "description": "OPTIONAL non-MCP caller fallback.",
            },
            "model": {
                "type": "string",
                "description": "OPTIONAL model override. Defaults to ANTHROPIC_DEFAULT_SONNET_MODEL.",
            },
            "max_frames": {
                "type": "integer",
                "default": 16,
                "minimum": 1,
                "maximum": 32,
            },
            "max_tokens": {
                "type": "integer",
                "default": 4096,
                "minimum": 256,
                "maximum": 16384,
            },
            "output_path": {
                "type": "string",
                "description": "OPTIONAL output path for the synthesized brief. Defaults "
                               "to <brief_path parent>/research_brief.json.",
            },
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "synthesis": {
                "type": "object",
                "properties": {
                    "status": {"enum": ["ok", "skipped", "failed"]},
                    "model": {"type": "string"},
                    "frames_used": {"type": "integer"},
                    "elapsed_seconds": {"type": "number"},
                    "fields_filled": {"type": "array", "items": {"type": "string"}},
                    "skip_reason": {"type": ["string", "null"]},
                },
            },
            "brief_path": {"type": "string"},
            "output_path": {"type": "string"},
            "fields_filled": {"type": "array"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=5,
        network_required=True,
    )

    idempotency_key_fields = ["brief_path", "model", "max_frames"]
    side_effects = [
        "writes synthesized brief to output_path",
        "calls external VLM endpoint",
    ]
    user_visible_verification = [
        "Open research_brief.json and confirm content_analysis.summary reads like a real summary, not a template placeholder.",
        "Confirm replication_guidance.key_elements_to_replicate is grounded in visible frames + transcript, not generic.",
    ]

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _load_frames(frames_dir: Path | None, brief: dict, max_frames: int) -> list[dict]:
        """Pick frames and base64-encode them for the VLM payload.

        Strategy: prefer explicit frames_dir listing (sorted by name);
        fall back to brief['keyframes'][*]['path'] in declared order. We
        evenly subsample to max_frames so the prompt never blows past the
        token budget.
        """
        candidates: list[Path] = []
        if frames_dir and frames_dir.is_dir():
            candidates = sorted(frames_dir.glob("*.jpg")) + sorted(frames_dir.glob("*.jpeg"))
            candidates += sorted(frames_dir.glob("*.png"))
        if not candidates:
            for kf in brief.get("keyframes", []):
                p = kf.get("path") if isinstance(kf, dict) else None
                if p and Path(p).exists():
                    candidates.append(Path(p))
        # Even subsample
        if len(candidates) > max_frames:
            step = len(candidates) / max_frames
            indices = [int(i * step) for i in range(max_frames)]
            candidates = [candidates[i] for i in indices]

        out = []
        for p in candidates[:max_frames]:
            try:
                b64 = base64.standard_b64encode(p.read_bytes()).decode("ascii")
                # qwen-vl-plus / Claude API image content blocks want a media type
                ext = p.suffix.lower()
                media_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
                    ext.lstrip("."), "image/jpeg"
                )
                out.append({
                    "path": str(p),
                    "name": p.name,
                    "media_type": media_type,
                    "data": b64,
                })
            except Exception:
                continue
        return out

    @staticmethod
    def _load_transcript(transcript_path: Path | None, brief_path: Path) -> tuple[str, list[dict]]:
        """Return (full_text, segments). Looks next to the brief if not given."""
        if transcript_path is None:
            candidates = [
                brief_path.parent / "_audio_transcript.json",
                brief_path.parent / "transcript.json",
                brief_path.parent / "transcript.txt",
                brief_path.parent.parent / "transcript.txt",
            ]
            for c in candidates:
                if c.exists():
                    transcript_path = c
                    break
        if transcript_path is None or not transcript_path.exists():
            # Fall back to brief's own narration_transcript
            return "", []
        try:
            if transcript_path.suffix == ".json":
                data = json.loads(transcript_path.read_text(encoding="utf-8"))
                segs = data.get("segments") or data.get("transcript") or []
                full = data.get("text") or data.get("full_text") or " ".join(
                    s.get("text", "") for s in segs
                )
                return full.strip(), segs
            else:
                return transcript_path.read_text(encoding="utf-8").strip(), []
        except Exception:
            return "", []

    @staticmethod
    def _call_vlm(
        endpoint: str,
        api_key: str,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
    ) -> str:
        """POST to /v1/messages. Returns the assistant text."""
        url = endpoint.rstrip("/") + "/v1/messages"
        body = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        })
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
        # Standard Claude response: content[0].text; reverse-proxies vary —
        # also accept {content: [{type:'text', text:'...'}]} shape.
        for block in data.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        # Fallback: some proxies return a flat string under "completion".
        if "completion" in data:
            return data["completion"]
        return json.dumps(data)[:2000]

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Robust JSON extractor. Strips ```json fences and finds the first {...} block."""
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        # Try direct parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # Find first {...} block
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return None
        candidate = m.group(0)
        # Handle nested braces by counting depth
        depth = 0
        end = 0
        in_str = False
        esc = False
        for i, ch in enumerate(candidate):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            try:
                return json.loads(candidate[:end])
            except Exception:
                return None
        return None

    @staticmethod
    def _build_prompt(brief: dict, transcript_text: str, frames_meta: list[dict]) -> tuple[str, list[dict]]:
        """Return (system, messages) for the VLM call."""
        pacing = brief.get("structure_analysis", {}).get("pacing_profile", {})
        scenes = brief.get("structure_analysis", {}).get("scenes", [])
        scene_summary = [
            {"scene_index": s.get("scene_index"), "start": s.get("start_time"),
             "end": s.get("end_time"), "duration_s": round(s.get("end_time", 0) - s.get("start_time", 0), 2)}
            for s in scenes[:30]
        ]
        source = brief.get("source", {})

        system = (
            "You are a senior short-video content strategist and color/typography analyst. "
            "You produce grounded, evidence-cited analysis based ONLY on the provided "
            "keyframes + transcript + structural metadata. Never invent details you cannot "
            "see in the frames or read in the transcript. Be concrete (cite frame N at "
            "mm:ss when relevant). Be opinionated about reuse-vs-replace — that is the "
            "most useful part of the output. Reply ONLY with a JSON object."
        )

        user_text_blocks = [
            {
                "type": "text",
                "text": (
                    f"# Reference video\n\n"
                    f"- duration: {source.get('duration_seconds')} s\n"
                    f"- platform: {source.get('type', 'unknown')}\n"
                    f"- title: {source.get('title', '')}\n\n"
                    f"# Pacing profile (from video_analyzer)\n"
                    f"{json.dumps(pacing, ensure_ascii=False, indent=2)}\n\n"
                    f"# Scene timeline (first {len(scene_summary)} of {len(scenes)} scenes)\n"
                    f"{json.dumps(scene_summary, ensure_ascii=False, indent=2)}\n\n"
                    f"# Transcript (Whisper, base, possibly noisy ASR)\n"
                    f"```\n{transcript_text[:6000]}\n```\n\n"
                    f"# Keyframes ({len(frames_meta)} images attached below)\n"
                    f"Each image is a scene-change moment. Note: captions burned into "
                    f"the frames are part of the source — read them when visible.\n\n"
                    f"# Fields to fill\n"
                    f"{json.dumps(SYNTHESIS_FIELDS, ensure_ascii=False, indent=2)}\n\n"
                    f"# Output\n"
                    f"Produce a JSON object with EXACTLY these keys (do not add or rename):\n"
                    f"```\n"
                    f"{{\n"
                    f'  "content_analysis": {{ "summary": "...", "topics": ["..."], "target_audience": "..." }},\n'
                    f'  "style_profile": {{\n'
                    f'    "color_palette": {{ "primary_colors": ["..."], "accent_colors": ["..."], "overall_mood": "..." }},\n'
                    f'    "typography_observed": "...",\n'
                    f'    "transition_types": ["..."],\n'
                    f'    "music_style": "...",\n'
                    f'    "subtitle_style": "...",\n'
                    f'    "production_quality": "professional|prosumer|amateur",\n'
                    f'    "closest_playbook": "...",\n'
                    f'    "playbook_delta": "...",\n'
                    f'    "narration_style": {{ "delivery_style": "..." }}\n'
                    f"  }},\n"
                    f'  "replication_guidance": {{\n'
                    f'    "key_elements_to_replicate": ["..."],\n'
                    f'    "elements_requiring_custom_work": ["..."],\n'
                    f'    "creative_differentiation_seeds": ["..."]\n'
                    f"  }}\n"
                    f"}}\n"
                    f"```\n\n"
                    f"Quality bar:\n"
                    f"- `key_elements_to_replicate` MUST reference form (cuts, captions, "
                    f"shot types), not content (which suitcase, which scene).\n"
                    f"- `elements_requiring_custom_work` MUST include any branded IP, "
                    f"unique personal style, or platform-specific watermark.\n"
                    f"- `creative_differentiation_seeds` must be 3-5 actionable idea seeds "
                    f"that would produce a recognizably different but adjacent video.\n"
                    f"- Avoid hedging language (\"it seems\", \"might be\"). State what you "
                    f"observe.\n"
                    f"- Keep total output under 1500 words.\n"
                ),
            }
        ]
        # Frames as image blocks
        for f in frames_meta:
            user_text_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": f["media_type"],
                    "data": f["data"],
                },
            })
            user_text_blocks.append({
                "type": "text",
                "text": f"(frame: {f['name']})\n",
            })
        return system, [{"role": "user", "content": user_text_blocks}]

    # ------------------------------------------------------------------ #
    # Execute                                                            #
    # ------------------------------------------------------------------ #

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        brief_path = Path(inputs["brief_path"])
        if not brief_path.exists():
            return ToolResult(success=False, error=f"brief_path not found: {brief_path}")
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8"))
        except Exception as e:
            return ToolResult(success=False, error=f"brief_path unreadable: {e}")

        endpoint = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        model = inputs.get("model") or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-5")

        if not endpoint or not api_key:
            return ToolResult(
                success=True,
                data={
                    "synthesis": {"status": "skipped",
                                  "skip_reason": "ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN not set"},
                    "brief_path": str(brief_path),
                    "output_path": None,
                    "fields_filled": [],
                },
                duration_seconds=0.0,
            )

        # Frames + transcript
        frames_dir = Path(inputs["frames_dir"]) if inputs.get("frames_dir") else None
        frames = self._load_frames(frames_dir, brief, int(inputs.get("max_frames", 16)))
        transcript_path = Path(inputs["transcript_path"]) if inputs.get("transcript_path") else None
        transcript_text, _segments = self._load_transcript(transcript_path, brief_path)

        start = time.time()
        try:
            system, messages = self._build_prompt(brief, transcript_text, frames)
            text = self._call_vlm(
                endpoint, api_key, model,
                int(inputs.get("max_tokens", 4096)),
                system, messages,
            )
        except urllib.error.HTTPError as e:
            return ToolResult(
                success=False,
                error=f"VLM HTTP {e.code}: {e.read().decode(errors='replace')[:300]}",
                data={"synthesis": {"status": "failed", "elapsed_seconds": round(time.time() - start, 2)}},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"VLM call failed: {e}",
                data={"synthesis": {"status": "failed", "elapsed_seconds": round(time.time() - start, 2)}},
            )

        parsed = self._extract_json(text)
        if not parsed:
            return ToolResult(
                success=False,
                error="VLM returned no parseable JSON",
                data={
                    "synthesis": {
                        "status": "failed",
                        "model": model,
                        "frames_used": len(frames),
                        "elapsed_seconds": round(time.time() - start, 2),
                        "raw_text_preview": text[:500],
                    }
                },
            )

        # Merge into brief
        fields_filled = []
        ca = parsed.get("content_analysis") or {}
        sp = parsed.get("style_profile") or {}
        rg = parsed.get("replication_guidance") or {}

        if ca.get("summary"):
            brief.setdefault("content_analysis", {})["summary"] = ca["summary"]; fields_filled += ["content_analysis.summary"]
        if ca.get("topics"):
            brief["content_analysis"]["topics"] = ca["topics"]; fields_filled += ["content_analysis.topics"]
        if ca.get("target_audience"):
            brief["content_analysis"]["target_audience"] = ca["target_audience"]; fields_filled += ["content_analysis.target_audience"]

        brief.setdefault("style_profile", {})
        if sp.get("color_palette"):
            brief["style_profile"]["color_palette"] = sp["color_palette"]; fields_filled += ["style_profile.color_palette"]
        for k in ("typography_observed", "transition_types", "music_style",
                  "subtitle_style", "production_quality",
                  "closest_playbook", "playbook_delta"):
            if sp.get(k) not in (None, "", []):
                brief["style_profile"][k] = sp[k]
                fields_filled += [f"style_profile.{k}"]
        if sp.get("narration_style"):
            brief["style_profile"].setdefault("narration_style", {})
            brief["style_profile"]["narration_style"].update(sp["narration_style"])
            fields_filled += ["style_profile.narration_style"]

        if rg.get("key_elements_to_replicate"):
            brief.setdefault("replication_guidance", {})["key_elements_to_replicate"] = rg["key_elements_to_replicate"]
            fields_filled += ["replication_guidance.key_elements_to_replicate"]
        if rg.get("elements_requiring_custom_work"):
            brief["replication_guidance"]["elements_requiring_custom_work"] = rg["elements_requiring_custom_work"]
            fields_filled += ["replication_guidance.elements_requiring_custom_work"]
        if rg.get("creative_differentiation_seeds"):
            brief["replication_guidance"]["creative_differentiation_seeds"] = rg["creative_differentiation_seeds"]
            fields_filled += ["replication_guidance.creative_differentiation_seeds"]

        # Stash transcript if we have one and brief doesn't
        if transcript_text and not brief.get("narration_transcript"):
            brief["narration_transcript"] = {
                "full_text": transcript_text,
                "language": "auto",
                "word_count": len(transcript_text),
                "source": "external_attachment",
            }
            brief.setdefault("_analysis_meta", {})["has_transcript"] = True

        # Mark synthesis provenance
        brief.setdefault("_analysis_meta", {})
        brief["_analysis_meta"]["synthesis"] = {
            "model": model,
            "endpoint": endpoint,
            "frames_used": len(frames),
            "elapsed_seconds": round(time.time() - start, 2),
            "fields_filled": fields_filled,
        }

        # Write
        output_path = Path(inputs["output_path"]) if inputs.get("output_path") else brief_path.parent / "research_brief.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

        return ToolResult(
            success=True,
            data={
                "synthesis": {
                    "status": "ok",
                    "model": model,
                    "frames_used": len(frames),
                    "elapsed_seconds": round(time.time() - start, 2),
                    "fields_filled": fields_filled,
                },
                "brief_path": str(brief_path),
                "output_path": str(output_path),
                "fields_filled": fields_filled,
            },
            artifacts=[str(output_path)],
            duration_seconds=round(time.time() - start, 2),
        )