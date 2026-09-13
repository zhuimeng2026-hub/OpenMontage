// Package auth implements HS256 JWT signing/verification using only stdlib.
// Used by Phase 0 of the MVP §17.A implementation.
package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const (
	defaultTTL = 24 * time.Hour
)

// Claims is the minimal payload we care about.
type Claims struct {
	InternalUserID string `json:"sub"`     // subject = internal_user_id
	OpenID         string `json:"openid,omitempty"`
	ExpiresAt      int64  `json:"exp"`     // unix seconds
	IssuedAt       int64  `json:"iat"`     // unix seconds
}

// Sign produces a compact JWT string using HS256.
func Sign(secret []byte, internalUserID, openID string, ttl time.Duration) (string, error) {
	if internalUserID == "" {
		return "", errors.New("internalUserID required")
	}
	now := time.Now()
	if ttl <= 0 {
		ttl = defaultTTL
	}
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	headerJSON, _ := json.Marshal(header)
	claims := Claims{
		InternalUserID: internalUserID,
		OpenID:         openID,
		ExpiresAt:      now.Add(ttl).Unix(),
		IssuedAt:       now.Unix(),
	}
	claimsJSON, _ := json.Marshal(claims)
	signingInput := b64(headerJSON) + "." + b64(claimsJSON)
	sig := sign(secret, signingInput)
	return signingInput + "." + sig, nil
}

// Verify parses + verifies a JWT and returns the claims.
func Verify(secret []byte, token string) (*Claims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("malformed token")
	}
	signingInput := parts[0] + "." + parts[1]
	wantSig := sign(secret, signingInput)
	if !hmac.Equal([]byte(wantSig), []byte(parts[2])) {
		return nil, errors.New("bad signature")
	}
	claimsJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("bad claims b64: %w", err)
	}
	var c Claims
	if err := json.Unmarshal(claimsJSON, &c); err != nil {
		return nil, fmt.Errorf("bad claims json: %w", err)
	}
	if time.Now().Unix() >= c.ExpiresAt {
		return nil, errors.New("token expired")
	}
	if c.InternalUserID == "" {
		return nil, errors.New("empty subject")
	}
	return &c, nil
}

// NewInternalUserID returns a random 16-byte hex string (collision-resistant
// enough for MVP).
func NewInternalUserID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "iu_" + hex.EncodeToString(b)
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func sign(secret []byte, signingInput string) string {
	m := hmac.New(sha256.New, secret)
	m.Write([]byte(signingInput))
	return b64(m.Sum(nil))
}

// SHA256Hex is a tiny helper kept here for tests that want to seed secret deterministically.
func SHA256Hex(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}
