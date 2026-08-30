// Package main — Phase 1 tenant CRUD handlers.
// Routes registered in main.go; protected by RequireJWT (no X-Tenant-Id
// required for Create/ListMine, because the caller doesn't have a tenant yet).
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// TenantHandler exposes tenant CRUD for the Phase 1 MVP.
type TenantHandler struct {
	DB *sql.DB
}

// NewTenantHandler is the canonical constructor.
func NewTenantHandler(db *sql.DB) *TenantHandler { return &TenantHandler{DB: db} }

type createTenantReq struct {
	Name string `json:"name" binding:"required"`
}

type tenantResp struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Status string `json:"status"`
	Role   string `json:"role"`
}

// Create handles POST /api/tenants — creates a tenant, binds the caller as owner.
// Auth: JWT only (no X-Tenant-Id needed pre-creation).
func (h *TenantHandler) Create(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	if uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing internal_user_id"})
		return
	}
	var req createTenantReq
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Name) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "name required"})
		return
	}
	id := newTenantID()
	if _, err := h.DB.Exec(
		`INSERT INTO tenants (id, name, created_by) VALUES (?, ?, ?)`,
		id, req.Name, uid,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create tenant failed: " + err.Error()})
		return
	}
	if _, err := h.DB.Exec(
		`INSERT INTO tenant_users (tenant_id, user_id, role) VALUES (?, ?, 'owner')`,
		id, uid,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "owner bind failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, tenantResp{ID: id, Name: req.Name, Status: "active", Role: "owner"})
}

// ListMine handles GET /api/tenants — lists tenants the caller belongs to.
// Auth: JWT only.
func (h *TenantHandler) ListMine(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	if uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing internal_user_id"})
		return
	}
	rows, err := h.DB.Query(
		`SELECT t.id, t.name, t.status, tu.role
		 FROM tenants t JOIN tenant_users tu ON t.id = tu.tenant_id
		 WHERE tu.user_id = ?
		 ORDER BY t.created_at DESC`,
		uid,
	)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list failed: " + err.Error()})
		return
	}
	defer rows.Close()

	out := []tenantResp{}
	for rows.Next() {
		var r tenantResp
		if err := rows.Scan(&r.ID, &r.Name, &r.Status, &r.Role); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "scan failed: " + err.Error()})
			return
		}
		out = append(out, r)
	}
	c.JSON(http.StatusOK, gin.H{"tenants": out, "count": len(out)})
}

type addMemberReq struct {
	UserID string `json:"user_id" binding:"required"`
	Role   string `json:"role"`
}

// AddMember handles POST /api/tenants/:id/members — adds a member to a tenant.
// Auth: JWT + X-Tenant-Id; only owner of the tenant may add members.
func (h *TenantHandler) AddMember(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	tid := c.Param("id")

	var callerRole string
	err := h.DB.QueryRow(
		`SELECT role FROM tenant_users WHERE tenant_id = ? AND user_id = ?`,
		tid, uid,
	).Scan(&callerRole)
	if errors.Is(err, sql.ErrNoRows) {
		c.JSON(http.StatusForbidden, gin.H{"error": "not a member of tenant"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if callerRole != "owner" {
		c.JSON(http.StatusForbidden, gin.H{"error": "only owner can add members"})
		return
	}

	var req addMemberReq
	if err := c.ShouldBindJSON(&req); err != nil || req.UserID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "user_id required"})
		return
	}
	if req.Role == "" {
		req.Role = "member"
	}
	if _, err := h.DB.Exec(
		`INSERT OR IGNORE INTO tenant_users (tenant_id, user_id, role) VALUES (?, ?, ?)`,
		tid, req.UserID, req.Role,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "add failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"tenant_id": tid,
		"user_id":   req.UserID,
		"role":      req.Role,
	})
}

func newTenantID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "tn_" + hex.EncodeToString(b)
}
