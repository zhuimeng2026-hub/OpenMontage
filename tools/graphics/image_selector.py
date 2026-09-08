"""Capability-level image selector that routes between generation and stock providers.

Provider discovery is automatic — any BaseTool with capability="image_generation"
is picked up from the registry.  Adding a new image provider requires only creating
the tool file in tools/graphics/; no changes to this selector are needed.
"""

from __future__ import annotations

from typing import Any

from tools.base_tool import BaseTool, ToolResult, ToolRuntime, ToolStability, ToolStatus, ToolTier


class ImageSelector(BaseTool):
    name = "image_selector"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "selector"
    stability = ToolStability.BETA
    runtime = ToolRuntime.HYBRID
    agent_skills = ["flux-best-practices", "bfl-api"]

    capabilities = [
        "generate_image", "search_image", "download_image",
        "provider_selection", "text_to_image", "stock_image",
    ]
    supports = {
        "user_preference_routing": True,
        "offline_fallback": True,
        "stock_fallback": True,
    }
    best_for = [
        "preflight routing — pick the best image provider for the task",
        "switching between generated and stock images",
        "automatic fallback when preferred provider is unavailable",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Image description (used as prompt for generation or query for stock)",
            },
            "negative_prompt": {
                "type": "string",
                "description": "What to avoid in the generated image. Passed to providers that support it.",
            },
            "width": {"type": "integer", "description": "Image width in pixels"},
            "height": {"type": "integer", "description": "Image height in pixels"},
            "seed": {"type": "integer", "description": "Random seed for reproducibility (generation providers only)"},
            "n": {"type": "integer", "description": "Number of image variations to request when supported."},
            "aspect_ratio": {
                "type": "string",
                "description": "Aspect ratio hint for providers that support ratio-based generation.",
            },
            "resolution": {
                "type": "string",
                "description": "Resolution tier for providers that support named resolutions.",
            },
            "api_family": {
                "type": "string",
                "description": "Provider-specific API family hint passed through when supported.",
            },
            "model_name": {
                "type": "string",
                "description": "Provider-specific model name passed through when supported.",
            },
            "generation_mode": {
                "type": "string",
                "enum": ["generate", "edit"],
                "default": "generate",
                "description": "Use 'edit' when providing one or more source images.",
            },
            "image_url": {"type": "string", "description": "Single source image URL for edit-capable providers."},
            "image_path": {"type": "string", "description": "Single local source image path for edit-capable providers."},
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Multiple source image URLs for compositing edits.",
            },
            "image_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Multiple local source image paths for compositing edits.",
            },
            "image_list": {
                "type": "array",
                "description": "Provider-specific image reference list, e.g. Kling Official Image Omni.",
            },
            "element_list": {
                "type": "array",
                "description": "Provider-specific element references, e.g. Kling Official element_id objects.",
            },
            "image_reference": {
                "type": "string",
                "description": "Provider-specific reference type, e.g. subject or face.",
            },
            "image_fidelity": {
                "type": "number",
                "description": "Provider-specific reference image fidelity hint.",
            },
            "human_fidelity": {
                "type": "number",
                "description": "Provider-specific human or face fidelity hint.",
            },
            "result_type": {
                "type": "string",
                "description": "Provider-specific result type, e.g. single or series.",
            },
            "series_amount": {
                "type": "string",
                "description": "Provider-specific series amount for image series generation.",
            },
            "watermark": {
                "type": "boolean",
                "description": "Provider-specific watermark toggle passed through when supported.",
            },
            "callback_url": {
                "type": "string",
                "description": "Provider-specific callback URL. Current OpenMontage providers still poll by default.",
            },
            "external_task_id": {
                "type": "string",
                "description": "Provider-specific idempotency/provenance task id.",
            },
            "preferred_provider": {
                "type": "string",
                "description": "Provider name or 'auto'. Valid values are discovered at runtime from the registry.",
                "default": "auto",
            },
            "allowed_providers": {
                "type": "array",
                "items": {"type": "string"},
            },
            "operation": {
                "type": "string",
                "enum": ["generate", "rank"],
                "default": "generate",
                "description": "Operation mode. 'rank' returns scored provider rankings without generating.",
            },
            "workflow_json": {
                "type": "string",
                "description": (
                    "Optional full ComfyUI workflow JSON. Routes to a custom-workflow-capable "
                    "provider (e.g. comfyui_image) based on server availability, not bundled "
                    "model readiness. Requires output_node."
                ),
            },
            "workflow_path": {
                "type": "string",
                "description": (
                    "Optional path to a ComfyUI workflow JSON file. Routes to a custom-workflow-"
                    "capable provider based on server availability. Requires output_node."
                ),
            },
            "output_node": {
                "type": "string",
                "description": "ComfyUI output node ID for a custom workflow_json/workflow_path.",
            },
            "workflow_name": {
                "type": "string",
                "description": "Optional human-readable provenance label for a custom workflow.",
            },
            "workflow_model": {
                "type": "string",
                "description": "Optional model/provenance label for a custom workflow.",
            },
            "workflow_model_stack": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional provenance metadata for custom workflow dependencies.",
            },
            "output_path": {"type": "string"},
            "project_id": {
                "type": "string",
                "description": "Project workspace to own the generated image artifact.",
            },
        },
    }

    def _providers(self) -> list[BaseTool]:
        """Auto-discover image generation providers from the registry."""
        from tools.tool_registry import registry
        registry.ensure_discovered()
        return [t for t in registry.get_by_capability("image_generation")
                if t.name != self.name]

    @property
    def fallback_tools(self) -> list[str]:
        """Dynamically built from discovered providers."""
        return [t.name for t in self._providers()]

    @property
    def provider_matrix(self) -> dict[str, dict[str, str]]:
        """Built at runtime from each provider's best_for field."""
        matrix = {}
        for tool in self._providers():
            strength = ", ".join(tool.best_for) if tool.best_for else tool.name
            matrix[tool.provider] = {"tool": tool.name, "strength": strength}
        return matrix

    def get_status(self) -> ToolStatus:
        if any(tool.get_status() == ToolStatus.AVAILABLE for tool in self._providers()):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        candidates = self._providers()
        if not candidates:
            return 0.0
        tool, _ = self._select_best_tool(inputs, candidates, self._prepare_task_context(inputs))
        return tool.estimate_cost(inputs) if tool else 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import logging
        from lib.scoring import rank_providers

        logger = logging.getLogger(__name__)
        # Generated images are later read through read_session_asset.  That
        # reader intentionally rejects projects/_scratch and every other
        # user's namespace, so never pass a caller-supplied scratch path to a
        # provider.  Re-home the artifact inside the authenticated principal's
        # project workspace before invoking the provider.
        from pathlib import Path
        from lib.principal_registry import Principal, PrincipalNotFound
        from lib.project_workspace import ProjectWorkspace

        prepared_inputs = dict(inputs)
        project_id = str(prepared_inputs.get("project_id") or "generated")
        # Mirror video_compose.execute(): allow explicit ``userid`` so non-MCP
        # callers (CLI, cron, smoke tests) can route to a specific workspace
        # without an MCP session binding.  MCP clients still get the standard
        # ``for_current_principal`` path via the registered contextvar.
        explicit_userid = prepared_inputs.get("userid")
        try:
            if explicit_userid:
                principal = Principal(kind="user", principal_id=explicit_userid)
                workspace = ProjectWorkspace.for_principal(principal, project_id)
            else:
                workspace = ProjectWorkspace.for_current_principal(project_id)
        except PrincipalNotFound as e:
            return ToolResult(
                success=False,
                error=(
                    f"no authenticated principal bound to this call: {e}. "
                    "Send X-VClaw-User-Id on the MCP request, or pass "
                    "userid explicitly when calling from a non-MCP context."
                ),
            )
        requested_output = str(prepared_inputs.get("output_path") or "generated.png")
        filename = Path(requested_output.replace("\\", "/")).name or "generated.png"
        output_path = workspace.artifacts / "image_gen" / "keyframes" / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        prepared_inputs["output_path"] = str(output_path)
        prepared_inputs.pop("project_id", None)
        prepared_inputs.pop("userid", None)
        task_context = self._prepare_task_context(inputs)
        candidates = self._filter_candidates(prepared_inputs, self._providers())

        # Rank mode — return scored provider rankings without generating
        if inputs.get("operation") == "rank":
            rankings = rank_providers(candidates, task_context)
            return ToolResult(
                success=True,
                data={
                    "rankings": self._serialize_rankings(candidates, rankings),
                    "explanation": "\n".join(r.explain() for r in rankings[:5]),
                    "normalized_task_context": task_context,
                },
            )

        # Normal generation — use scored selection
        tool, score = self._select_best_tool(prepared_inputs, candidates, task_context)
        if tool is None:
            return ToolResult(success=False, error="No image provider available.")

        # Adapt input keys: stock tools use 'query' while generators use 'prompt'
        adapted = dict(prepared_inputs)
        if hasattr(tool, 'input_schema'):
            props = tool.input_schema.get("properties", {})
            if "query" in props and "query" not in adapted:
                adapted["query"] = adapted.get("prompt", "")

        # Strip selector-only keys that downstream tools don't understand
        adapted.pop("preferred_provider", None)
        adapted.pop("allowed_providers", None)

        # Pass through generation params only to tools that accept them.
        if hasattr(tool, 'input_schema'):
            props = tool.input_schema.get("properties", {})
            stripped = []
            for passthrough_key in (
                "negative_prompt",
                "width",
                "height",
                "seed",
                "n",
                "aspect_ratio",
                "resolution",
                "generation_mode",
                "image_url",
                "image_path",
                "image_urls",
                "image_paths",
                "image_list",
                "element_list",
                "api_family",
                "model_name",
                "image_reference",
                "image_fidelity",
                "human_fidelity",
                "result_type",
                "series_amount",
                "watermark",
                "callback_url",
                "external_task_id",
                "workflow_json",
                "workflow_path",
                "output_node",
                "workflow_name",
                "workflow_model",
                "workflow_model_stack",
            ):
                if passthrough_key in adapted and passthrough_key not in props:
                    stripped.append(f"{passthrough_key}={adapted.pop(passthrough_key)}")
            if stripped:
                logger.warning(
                    "image_selector: stripped unsupported params for %s: %s",
                    tool.name, ", ".join(stripped),
                )

        result = tool.execute(adapted)
        if result.success:
            # Public image_selector contract: outputs is always the only
            # artifact field consumed by clients, including for one image.
            # Providers in the registry have historically returned output,
            # outputs, image_paths, or ToolResult.artifacts; normalize that
            # variation at this selector boundary.
            result_data = result.data if isinstance(result.data, dict) else {}
            normalized_outputs: list[str] = []

            def add_output(value: Any) -> None:
                if isinstance(value, str) and value.strip():
                    normalized_outputs.append(value.strip())
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        add_output(item)

            add_output(result_data.get("outputs"))
            add_output(result_data.get("output"))
            add_output(result_data.get("image_paths"))
            add_output(result.artifacts)
            result_data["outputs"] = list(dict.fromkeys(normalized_outputs))
            result_data.pop("output", None)
            result.data = result_data
            result.data.setdefault("selected_tool", tool.name)
            result.data["selected_provider"] = tool.provider
            result.data["selection_reason"] = score.explain() if score else f"Selected {tool.provider} ({tool.name})"
            if score:
                result.data["provider_score"] = score.to_dict()
            result.data.update(self._tool_context_payload(tool))
            result.data["alternatives_considered"] = [
                t.name for t in candidates
                if t.name != tool.name and t.get_status().value == "available"
            ]
        return result

    def _select_best_tool(
        self,
        inputs: dict[str, Any],
        candidates: list[BaseTool],
        task_context: dict[str, Any],
    ) -> tuple[BaseTool | None, object]:
        """Select the best provider using scored ranking."""
        from lib.scoring import rank_providers

        preferred = inputs.get("preferred_provider", "auto")
        allowed = set(inputs.get("allowed_providers") or [])
        if allowed:
            candidates = [tool for tool in candidates if tool.provider in allowed]
        candidates = self._filter_candidates(inputs, candidates)

        rankings = rank_providers(candidates, task_context)

        tool_by_provider: dict[str, BaseTool] = {}
        for tool in candidates:
            if tool.provider not in tool_by_provider and self._tool_selectable(tool, inputs):
                tool_by_provider[tool.provider] = tool

        if preferred != "auto":
            for score_item in rankings:
                if score_item.provider == preferred and score_item.provider in tool_by_provider:
                    return tool_by_provider[score_item.provider], score_item

        for score_item in rankings:
            if score_item.provider in tool_by_provider:
                return tool_by_provider[score_item.provider], score_item

        return None, None

    def _prepare_task_context(self, inputs: dict[str, Any]) -> dict[str, Any]:
        from lib.scoring import normalize_task_context

        return normalize_task_context(
            inputs.get("task_context", {}),
            prompt=inputs.get("prompt", ""),
            capability=self.capability,
            operation=inputs.get("generation_mode", inputs.get("operation", "generate")),
        )

    @staticmethod
    def _tool_context_payload(tool: BaseTool) -> dict[str, Any]:
        info = tool.get_info()
        return {
            "selected_tool_agent_skills": info.get("agent_skills", []),
            "required_agent_skills": info.get("agent_skills", []),
            "selected_tool_usage_location": info.get("usage_location"),
            "selected_tool_best_for": info.get("best_for", []),
        }

    def _serialize_rankings(self, candidates: list[BaseTool], rankings: list[object]) -> list[dict[str, Any]]:
        tool_by_name = {tool.name: tool for tool in candidates}
        serialized: list[dict[str, Any]] = []
        for score in rankings:
            item = score.to_dict()
            tool = tool_by_name.get(score.tool_name)
            if tool:
                info = tool.get_info()
                item["agent_skills"] = info.get("agent_skills", [])
                item["usage_location"] = info.get("usage_location")
                item["best_for"] = info.get("best_for", [])
                item["supports"] = info.get("supports", {})
                item["status"] = str(tool.get_status())
            serialized.append(item)
        return serialized

    def _filter_candidates(self, inputs: dict[str, Any], candidates: list[BaseTool]) -> list[BaseTool]:
        # A caller-supplied custom workflow is provider-specific (ComfyUI graph
        # JSON). Route it only to custom-workflow-capable providers whose server
        # is reachable — bundled-model readiness is irrelevant in that case.
        if self._has_custom_workflow(inputs):
            return [t for t in candidates if self._custom_workflow_eligible(t, inputs)]

        wants_edit = (
            inputs.get("generation_mode") == "edit"
            or inputs.get("image_url")
            or inputs.get("image_path")
            or inputs.get("image_urls")
            or inputs.get("image_paths")
        )
        if not wants_edit:
            return candidates

        filtered: list[BaseTool] = []
        for tool in candidates:
            props = getattr(tool, "input_schema", {}).get("properties", {})
            supports = getattr(tool, "supports", {})
            if supports.get("image_edit") or any(
                key in props for key in ("image", "images", "image_url", "image_path", "image_urls", "image_paths")
            ):
                filtered.append(tool)
        return filtered or candidates

    @staticmethod
    def _has_custom_workflow(inputs: dict[str, Any]) -> bool:
        return bool(inputs.get("workflow_json") or inputs.get("workflow_path"))

    def _custom_workflow_eligible(self, tool: BaseTool, inputs: dict[str, Any]) -> bool:
        """Whether a tool can run the caller-supplied custom workflow.

        Eligibility is based on server availability, not bundled-model readiness:
        a provider qualifies when it advertises ``custom_workflow`` support, an
        ``output_node`` is supplied, and its backend is reachable (status is not
        UNAVAILABLE).
        """
        if not self._has_custom_workflow(inputs):
            return False
        if not inputs.get("output_node"):
            return False
        supports = getattr(tool, "supports", {})
        if not supports.get("custom_workflow"):
            return False
        return tool.get_status() != ToolStatus.UNAVAILABLE

    def _tool_selectable(self, tool: BaseTool, inputs: dict[str, Any]) -> bool:
        """A provider is selectable if it is AVAILABLE, or if it can serve a
        caller-supplied custom workflow even while bundled models report DEGRADED."""
        if tool.get_status() == ToolStatus.AVAILABLE:
            return True
        return self._custom_workflow_eligible(tool, inputs)
