"""External service adapters — third-party integrations that send work TO OpenMontage.

This is the inbound counterpart to the vendored _comfyui / _kling adapters
(outbound: OpenMontage → those services). Modules under ``external/`` receive
a structured payload from an external tool's MCP server and translate it into
an OpenMontage pipeline trigger.

Currently:
    claude_video   — /watch analysis forwarded via the ``recompose`` MCP tool
                     over stdio. Spec: docs/claude-video-integration.md.
"""

from .claude_video import ClaudeVideoComposeTool

__all__ = ["ClaudeVideoComposeTool"]
