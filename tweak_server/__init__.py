"""OpenMontage tweak server — sidecar FastAPI app for end-user render tweaks.

A minimal MCP client that exposes a browser form for tweaking Remotion render
props (theme, per-cut text/colors/animation, audio volumes) and dispatches the
render through the local OpenMontage MCP server (port 8900) via JSON-RPC.

Architecture: see docs/plans/rosy-dazzling-bear.md (rooted at /root/.claude/plans/).
Does NOT touch the MCP server or openclaw-gateway.
"""

__version__ = "0.1.0"