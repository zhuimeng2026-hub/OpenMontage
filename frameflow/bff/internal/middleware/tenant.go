package middleware

import (
	"database/sql"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
)

// TenantScope aborts with 401 when X-Tenant-Id is missing or 403 when the
// authenticated user (looked up from gin.Context key "internal_user_id"
// populated by RequireJWT) is not a member of the named tenant.
//
// On success it sets `tenant_id` and `role` on the gin.Context.
//
// MUST be chained AFTER RequireJWT — without it, internal_user_id is absent.
func TenantScope(db *sql.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		uidV, ok := c.Get("internal_user_id")
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "missing internal_user_id (RequireJWT must run first)",
			})
			return
		}
		uid, _ := uidV.(string)

		tid := c.GetHeader("X-Tenant-Id")
		if tid == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "X-Tenant-Id header required",
			})
			return
		}

		var role string
		err := db.QueryRow(
			`SELECT role FROM tenant_users WHERE tenant_id = ? AND user_id = ?`,
			tid, uid,
		).Scan(&role)
		if errors.Is(err, sql.ErrNoRows) {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
				"error": "not a member of tenant",
			})
			return
		}
		if err != nil {
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{
				"error": "tenant lookup failed: " + err.Error(),
			})
			return
		}

		c.Set("tenant_id", tid)
		c.Set("role", role)
		c.Next()
	}
}
