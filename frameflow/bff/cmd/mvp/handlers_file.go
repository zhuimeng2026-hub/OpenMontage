// Package main — Phase 1 file sign + serve handlers.
package main

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/filesvc"
)

// FileHandler exposes /api/files/sign (mint) and /api/files/:key (serve).
type FileHandler struct {
	DB *sql.DB
}

func NewFileHandler(db *sql.DB) *FileHandler { return &FileHandler{DB: db} }

// Sign handles GET /api/files/sign?key=<file_key>[&ttl_seconds=N] — mints a
// signed URL for the named file. Caller must be in the tenant that owns the
// file (X-Tenant-Id + tenant_users lookup).
func (h *FileHandler) Sign(c *gin.Context) {
	fileKey := c.Query("key")
	if fileKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "key query param required"})
		return
	}
	tidV, ok := c.Get("tenant_id")
	if !ok {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "tenant_id missing from context (TenantScope must run first)"})
		return
	}
	tid := tidV.(string)

	actualTid, err := filesvc.LookupTenant(c.Request.Context(), h.DB, fileKey)
	if errors.Is(err, filesvc.ErrFileNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "file not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "acl lookup: " + err.Error()})
		return
	}
	if actualTid != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "file belongs to another tenant"})
		return
	}

	ttl := 5 * time.Minute
	if v := c.Query("ttl_seconds"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 3600 {
			ttl = time.Duration(n) * time.Second
		}
	}
	exp, sig := filesvc.SignURL(filesvc.SecretBytes(), fileKey, ttl)
	c.JSON(http.StatusOK, gin.H{
		"file_key":    fileKey,
		"exp":         exp,
		"sig":         sig,
		"ttl_seconds": int(ttl.Seconds()),
		"url":         fmt.Sprintf("/api/files/%s?exp=%d&sig=%s", fileKey, exp, sig),
	})
}

// Serve handles GET /api/files/:key?exp=<unix>&sig=<hex> — serves file
// bytes (placeholder for MVP). No JWT required: the URL itself is the
// authorization. Verify checks sig + exp; ACL re-check confirms the file
// is still bound (avoids serving rows whose file_key was re-bound mid-flight).
func (h *FileHandler) Serve(c *gin.Context) {
	fileKey := c.Param("key")
	expStr := c.Query("exp")
	sig := c.Query("sig")
	if expStr == "" || sig == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "exp and sig required"})
		return
	}
	exp, err := strconv.ParseInt(expStr, 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid exp"})
		return
	}
	if err := filesvc.Verify(filesvc.SecretBytes(), fileKey, exp, sig); err != nil {
		c.JSON(http.StatusForbidden, gin.H{"error": "verify failed: " + err.Error()})
		return
	}
	tid, err := filesvc.LookupTenant(c.Request.Context(), h.DB, fileKey)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "file not found"})
		return
	}
	// MVP placeholder — real bytes are wired in Phase 2+ via object storage.
	c.JSON(http.StatusOK, gin.H{
		"file_key":  fileKey,
		"tenant_id": tid,
		"note":      "MVP placeholder — real bytes wired in Phase 2+ (object storage + signed URL cache)",
		"served_at": time.Now().Unix(),
	})
}
