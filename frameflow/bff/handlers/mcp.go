package handlers

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

// MCPProxy receives { "tool": "<name>", "args": { ... } } and forwards it to the
// OpenMontage MCP server as a tools/call, returning the extract()-ed structured
// result (mirrors om_mcp_probe.py). The caller's BFF session cookie selects the
// long-lived MCP client so uploads and the create call share one session.
func (h *Handlers) MCPProxy(c *gin.Context) {
	var req struct {
		Tool string                 `json:"tool"`
		Args map[string]interface{} `json:"args"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
		return
	}
	if req.Tool == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "tool is required"})
		return
	}
	sid := h.ensureSession(c)
	res, err := h.Store.Call(sid, req.Tool, req.Args)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, res)
}
