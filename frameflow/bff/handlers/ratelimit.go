package handlers

import (
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

// RateLimiter is an in-memory token-bucket limiter keyed by BFF session id
// (falling back to client IP). It is applied to the expensive upstream-facing
// routes (/api/mcp, /api/render-progress) so a single client cannot hammer the
// shared Remotion/MCP backend. For multi-instance deploys, swap this for a
// shared store (Redis) keyed the same way.
type RateLimiter struct {
	mu       sync.Mutex
	buckets  map[string]*bucket
	rate     float64 // tokens refilled per second
	capacity float64
}

type bucket struct {
	tokens float64
	last   time.Time
}

// NewRateLimiter builds a limiter with the given per-minute rate (per key).
// Capacity equals the per-minute rate so a client can burst up to that many
// requests, then refills continuously.
func NewRateLimiter(perMin int) *RateLimiter {
	if perMin <= 0 {
		perMin = 30
	}
	cap := float64(perMin)
	rl := &RateLimiter{
		buckets:  make(map[string]*bucket),
		rate:     float64(perMin) / 60.0,
		capacity: cap,
	}
	go rl.sweep()
	return rl
}

// Middleware returns the gin middleware.
func (rl *RateLimiter) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		now := time.Now()
		k := rl.key(c)
		rl.mu.Lock()
		b, ok := rl.buckets[k]
		if !ok {
			b = &bucket{tokens: rl.capacity, last: now}
			rl.buckets[k] = b
		}
		elapsed := now.Sub(b.last).Seconds()
		b.tokens += elapsed * rl.rate
		if b.tokens > rl.capacity {
			b.tokens = rl.capacity
		}
		b.last = now
		if b.tokens < 1 {
			rl.mu.Unlock()
			retry := int((1-b.tokens)/rl.rate) + 1
			c.Header("Retry-After", strconv.Itoa(retry))
			c.AbortWithStatusJSON(http.StatusTooManyRequests, gin.H{
				"error": "rate limit exceeded",
			})
			return
		}
		b.tokens--
		rl.mu.Unlock()
		c.Next()
	}
}

// key prefers the stable session cookie so a logged-in user is limited as a
// unit; anonymous clients are limited by IP.
func (rl *RateLimiter) key(c *gin.Context) string {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		return "s:" + sid
	}
	return "ip:" + c.ClientIP()
}

// sweep drops idle buckets so the map cannot grow unbounded.
func (rl *RateLimiter) sweep() {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()
	for range ticker.C {
		now := time.Now()
		rl.mu.Lock()
		for k, b := range rl.buckets {
			if now.Sub(b.last) > 15*time.Minute {
				delete(rl.buckets, k)
			}
		}
		rl.mu.Unlock()
	}
}
