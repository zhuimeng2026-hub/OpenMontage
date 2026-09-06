// Package middleware exposes thin Gin middleware wrappers reused by both
// the production BFF and the MVP standalone binary (cmd/mvp).
package middleware

import (
	"frameflow-bff/internal/auth"

	"github.com/gin-gonic/gin"
)

// RequireJWT returns a Gin middleware that enforces a Bearer JWT signed by
// the given JWTService. On success it sets `internal_user_id` and `openid`
// on the gin.Context (matching the keys auth.JWTService.JWTAuthMiddleware
// writes, so downstream code can stay agnostic of which auth path issued it).
func RequireJWT(jwtSvc *auth.JWTService) gin.HandlerFunc {
	return jwtSvc.JWTAuthMiddleware()
}
