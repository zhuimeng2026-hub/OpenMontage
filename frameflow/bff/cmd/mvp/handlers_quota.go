// Package main — Phase 4 §17.E quota / billing handlers.
//
// Four routes, mounted under the `scoped` group (RequireJWT + TenantScope):
//
//   GET  /api/quota             — read tenant quota (auto-upsert free tier)
//   POST /api/quota/reserve     — {amount, job_id} → reservation_id
//   POST /api/quota/consume     — {reservation_id} → consumed
//   POST /api/quota/refund      — {reservation_id} → refunded
//
// Errors: 400 (bad body), 402 (ErrInsufficient), 500 (db failure).
package main

import (
	"database/sql"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/quotasvc"
)

type QuotaHandler struct {
	DB *sql.DB
}

func NewQuotaHandler(db *sql.DB) *QuotaHandler { return &QuotaHandler{DB: db} }

// quotaIdentity reads tenant_id + internal_user_id off the gin context.
// Returns "" for either if missing — handlers map that to 401.
func quotaIdentity(c *gin.Context) (tid, uid string) {
	t, _ := c.Get("tenant_id")
	tid, _ = t.(string)
	u, _ := c.Get("internal_user_id")
	uid, _ = u.(string)
	return
}

func (h *QuotaHandler) Get(c *gin.Context) {
	tid, _ := quotaIdentity(c)
	if tid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "tenant_id missing"})
		return
	}
	q, err := quotasvc.GetOrInit(c.Request.Context(), h.DB, tid)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, q)
}

func (h *QuotaHandler) Reserve(c *gin.Context) {
	tid, uid := quotaIdentity(c)
	if tid == "" || uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "identity missing"})
		return
	}
	var req struct {
		Amount float64 `json:"amount"`
		JobID  string  `json:"job_id"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Amount <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "amount > 0 required"})
		return
	}
	rid, err := quotasvc.Reserve(c.Request.Context(), h.DB, tid, req.Amount, req.JobID, uid)
	if errors.Is(err, quotasvc.ErrInsufficient) {
		c.JSON(http.StatusPaymentRequired, gin.H{"error": "insufficient credits"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "reserve failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"reservation_id": rid,
		"amount":         req.Amount,
		"job_id":         req.JobID,
		"tenant_id":      tid,
	})
}

func (h *QuotaHandler) Consume(c *gin.Context) {
	tid, uid := quotaIdentity(c)
	if tid == "" || uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "identity missing"})
		return
	}
	var req struct {
		Amount float64 `json:"amount"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Amount <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "amount > 0 required"})
		return
	}
	if err := quotasvc.Consume(c.Request.Context(), h.DB, tid, req.Amount, uid); err != nil {
		if errors.Is(err, quotasvc.ErrInsufficient) {
			c.JSON(http.StatusConflict, gin.H{"error": "insufficient reserved credits"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "consume failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"status":  "consumed",
		"amount":  req.Amount,
		"tenant_id": tid,
	})
}

func (h *QuotaHandler) Refund(c *gin.Context) {
	tid, uid := quotaIdentity(c)
	if tid == "" || uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "identity missing"})
		return
	}
	var req struct {
		Amount float64 `json:"amount"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Amount <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "amount > 0 required"})
		return
	}
	if err := quotasvc.Refund(c.Request.Context(), h.DB, tid, req.Amount, uid); err != nil {
		if errors.Is(err, quotasvc.ErrInsufficient) {
			c.JSON(http.StatusConflict, gin.H{"error": "insufficient reserved credits"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "refund failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"status":  "refunded",
		"amount":  req.Amount,
		"tenant_id": tid,
	})
}
