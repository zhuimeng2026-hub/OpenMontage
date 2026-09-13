"""Capability-level translation selector.

Routes a translation request to a provider tool (e.g. argos_translator).
Provider discovery is automatic — any BaseTool with capability='translation'
in this package is picked up from the registry.

Adding a new provider (DeepL, DashScope, OpenAI, Claude) means dropping
a new file in tools/translation/ and importing it here for registration,
or relying on registry.discover() if the file is under the same package.
"""

from __future__ import annotations

from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)


class Translator(BaseTool):
    name = "translator"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "translation"
    provider = "selector"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    # NOTE: Determinism defaults to DETERMINISTIC. argostranslate is
    # greedy-decoded so identical input produces identical output.

    dependencies = []
    install_instructions = (
        "Install at least one provider (e.g. `pip install argostranslate` "
        "and download an en<->zh model)."
    )
    agent_skills = ["remotion-best-practices"]

    capabilities = [
        "translate_segments",
        "translate_text",
        "provider_selection",
    ]

    supports = {
        "user_preference_routing": True,
        "offline_fallback": True,
        "multilingual": True,
    }
    best_for = [
        "preflight tool selection",
        "user-facing recommendation flows",
    ]

    input_schema = {
        "type": "object",
        "oneOf": [
            {"required": ["segments"]},
            {"required": ["text"]},
        ],
        "properties": {
            "segments": {"type": "array"},
            "text": {"type": "string"},
            "source_lang": {
                "type": "string",
                "enum": ["en", "zh", "auto"],
                "default": "en",
            },
            "target_lang": {
                "type": "string",
                "enum": ["en", "zh"],
                "default": "zh",
            },
            "glossary": {"type": "object"},
            "preferred_provider": {
                "type": "string",
                "description": (
                    "Force a specific provider name (e.g. 'argos'). "
                    "When omitted, the first available provider wins."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=10)

    _PROVIDER_NAMES = ("argos_translator",)

    def _pick_provider(self, preferred: str | None) -> tuple[str, Any] | tuple[None, None]:
        try:
            from tools.tool_registry import registry
            from tools.base_tool import ToolStatus
        except Exception:
            return None, None

        if not getattr(registry, "_tools", None):
            registry.discover()

        candidates = [preferred] if preferred else list(self._PROVIDER_NAMES)
        for name in candidates:
            tool = registry._tools.get(name)
            if tool is None:
                continue
            try:
                status = tool.get_status()
            except Exception:
                continue
            if status == ToolStatus.AVAILABLE:
                return name, tool

        # Fallback: first registered translation tool that is available
        for name, tool in registry._tools.items():
            if getattr(tool, "capability", None) != "translation":
                continue
            if name == self.name:
                continue
            try:
                status = tool.get_status()
            except Exception:
                continue
            if status == ToolStatus.AVAILABLE:
                return name, tool
        return None, None

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        preferred = inputs.pop("preferred_provider", None)
        provider_name, tool = self._pick_provider(preferred)

        if tool is None:
            return ToolResult(
                success=False,
                error=(
                    "No translation provider available. Install argostranslate "
                    "and the en<->zh models (see tools/translation/argos_translator.py)."
                ),
            )

        # Forward to the provider with the remaining inputs.
        result = tool.execute(inputs)
        if result.success and isinstance(result.data, dict):
            result.data.setdefault("provider", provider_name)
        return result
