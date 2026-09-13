// Package filesvc implements signed-URL mint/verify and file_acl lookup.
//
// Signed URL format (no JWT required to serve):
//
//	GET /api/files/<file_key>?exp=<unix_seconds>&sig=<hex_hmac_sha256>
//
// sig = HMAC-SHA256(SecretBytes(), fileKey + ":" + exp)
//
// The secret is loaded at request time (not package init) so tests can
// override it via env without rebuilding.
package filesvc

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"strconv"
	"time"
)

// ErrExpired is returned by Verify when the URL's exp has passed.
var ErrExpired = errors.New("signed url expired")

// ErrBadSignature is returned by Verify when sig doesn't match the expected HMAC.
var ErrBadSignature = errors.New("bad signature")

// SecretBytes resolves the signing secret at call time.
// Precedence: FILESIGN_SECRET > JWT_SECRET > MVP_DEV seed (NEVER use the
// dev seed in production — rotate secrets regularly).
func SecretBytes() []byte {
	if v := os.Getenv("FILESIGN_SECRET"); v != "" {
		return []byte(v)
	}
	if v := os.Getenv("JWT_SECRET"); v != "" {
		return []byte(v)
	}
	return []byte("MVP_DEV_FILESIGN_SEED_DO_NOT_USE_IN_PROD")
}

// SignURL returns (exp, sig) for a signed download URL with the given TTL.
// exp is unix seconds; sig is hex-encoded HMAC-SHA256.
func SignURL(secret []byte, fileKey string, ttl time.Duration) (exp int64, sig string) {
	exp = time.Now().Add(ttl).Unix()
	sig = computeSig(secret, fileKey, exp)
	return
}

// Verify checks that sig matches HMAC-SHA256(secret, fileKey + ":" + exp)
// AND that exp is in the future. Returns nil on success.
func Verify(secret []byte, fileKey string, exp int64, sig string) error {
	want := computeSig(secret, fileKey, exp)
	if !hmac.Equal([]byte(want), []byte(sig)) {
		return ErrBadSignature
	}
	if time.Now().Unix() >= exp {
		return ErrExpired
	}
	return nil
}

func computeSig(secret []byte, fileKey string, exp int64) string {
	m := hmac.New(sha256.New, secret)
	m.Write([]byte(fileKey))
	m.Write([]byte(":"))
	m.Write([]byte(strconv.FormatInt(exp, 10)))
	return hex.EncodeToString(m.Sum(nil))
}
