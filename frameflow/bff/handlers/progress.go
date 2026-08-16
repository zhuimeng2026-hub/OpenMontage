package handlers

import (
	"fmt"
	"net/http"

	"github.com/gin-gonic/gin"
)

// RenderProgress proxies the upstream render-progress SSE stream down to the
// browser. The upstream authenticates with the Bearer token (server-side), so
// the browser needs no credential here — it just opens an EventSource to
// /api/render-progress/:jobId. We disable buffering and flush every chunk.
func (h *Handlers) RenderProgress(c *gin.Context) {
	jobID := c.Param("jobId")
	if jobID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "jobId required"})
		return
	}
	sid, err := c.Cookie(sessionCookieName)
	if err != nil || sid == "" || !h.Store.OwnsJob(sid, jobID) {
		c.JSON(http.StatusNotFound, gin.H{"error": "render job not found"})
		return
	}
	upstream := fmt.Sprintf("%s/%s", h.Cfg.MCPProgressURL, jobID)
	req, err := http.NewRequest(http.MethodGet, upstream, nil)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	req.Header.Set("Authorization", "Bearer "+h.Cfg.MCPAPIToken)
	req.Header.Set("Accept", "text/event-stream")

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "upstream error: " + err.Error()})
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		c.JSON(resp.StatusCode, gin.H{"error": "upstream status " + resp.Status})
		return
	}

	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")

	flusher, ok := c.Writer.(http.Flusher)
	if !ok {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "streaming unsupported"})
		return
	}

	buf := make([]byte, 4096)
	for {
		select {
		case <-c.Request.Context().Done():
			return
		default:
		}
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, werr := c.Writer.Write(buf[:n]); werr != nil {
				return
			}
			flusher.Flush()
		}
		if readErr != nil {
			return // io.EOF or client disconnect
		}
	}
}
