// Package quotasvc implements §17.E quota / billing.
//
//   quota_credits — one row per tenant (available / reserved / consumed / tier)
//   quota_ledger  — audit log; one row per reserve / consume / refund
//
// Invariant: available + reserved + consumed == tier limit (free = 100).
package quotasvc

const (
	TierFree = "free"
	TierPro  = "pro"
)

const DefaultFreeCredits = 100.0

const (
	OpReserve = "reserve"
	OpConsume = "consume"
	OpRefund  = "refund"
)

type Quota struct {
	TenantID         string  `json:"tenant_id"`
	AvailableCredits float64 `json:"available_credits"`
	ReservedCredits  float64 `json:"reserved_credits"`
	ConsumedCredits  float64 `json:"consumed_credits"`
	Tier              string  `json:"tier"`
}

type LedgerEntry struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	Operation    string  `json:"operation"`
	Amount       float64 `json:"amount"`
	JobID        string  `json:"job_id"`
	BalanceAfter string  `json:"balance_after"`
	CreatedBy    string  `json:"created_by"`
}
