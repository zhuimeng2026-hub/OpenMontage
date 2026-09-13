package quotasvc

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
)

// NewLedgerID mints a fresh reservation_id (= ledger row id).
// Format: "rs_" + 24 hex chars. Distinct prefix so quota reservations
// don't collide with job/tenant/product/file IDs in logs.
func NewLedgerID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "rs_" + hex.EncodeToString(b)
}

// BalanceSnapshot is the JSON shape stored in quota_ledger.balance_after.
type BalanceSnapshot struct {
	Available float64 `json:"available"`
	Reserved  float64 `json:"reserved"`
	Consumed  float64 `json:"consumed"`
}

// EncodeBalance marshals a snapshot. Returns "{}" on marshal failure —
// the audit row stays useful even without the snapshot.
func EncodeBalance(available, reserved, consumed float64) string {
	b, err := json.Marshal(BalanceSnapshot{
		Available: available, Reserved: reserved, Consumed: consumed,
	})
	if err != nil {
		return "{}"
	}
	return string(b)
}
