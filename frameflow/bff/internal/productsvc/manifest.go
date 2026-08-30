package productsvc

import (
	"context"
	"database/sql"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
)

// CommonRoles is the fixed 11-role set a "complete" product asset bundle
// should cover. Missing roles surface as gaps in the manifest so the user
// (or downstream agent) knows what to source next.
var CommonRoles = []string{
	"hero_front",
	"hero_45",
	"side",
	"back",
	"detail",
	"lifestyle",
	"logo",
	"open_view",
	"inside",
	"wheel_detail",
	"handle_detail",
}

// ManifestAsset is the per-asset row embedded in the manifest's assets_json.
// Kept as a separate type so the public surface is the same whether built
// from a fresh ListAssets or restored from a stored manifest JSON blob.
type ManifestAsset struct {
	AssetID      string  `json:"asset_id"`
	Role         string  `json:"role"`
	QualityScore float64 `json:"quality_score"`
	FileKey      string  `json:"file_key"`
}

// BuildManifest reads the current asset list for productID, computes which of
// the CommonRoles are missing, and returns a fresh Manifest row (NOT yet
// inserted — caller decides whether to persist via CreateManifest).
//
// BuildManifest is pure: it does NOT bump version based on existing rows.
// Callers that want a versioned history should query GetLatestManifest first
// and pass m.Version = last.Version + 1.
func BuildManifest(ctx context.Context, db *sql.DB, productID string) (Manifest, error) {
	assets, err := ListAssets(ctx, db, productID)
	if err != nil {
		return Manifest{}, err
	}

	seen := map[string]bool{}
	items := []ManifestAsset{}
	for _, a := range assets {
		items = append(items, ManifestAsset{
			AssetID:      a.ID,
			Role:         a.Role,
			QualityScore: a.QualityScore,
			FileKey:      a.FileKey,
		})
		if a.Role != "" && a.Role != "unclassified" {
			seen[a.Role] = true
		}
	}

	missing := []string{}
	for _, r := range CommonRoles {
		if !seen[r] {
			missing = append(missing, r)
		}
	}

	assetsJSON, _ := json.Marshal(items)
	missingJSON, _ := json.Marshal(missing)

	return Manifest{
		ID:              newManifestID(),
		ProductID:       productID,
		Version:         1, // caller bumps
		AssetsJSON:      string(assetsJSON),
		MissingRolesJSON: string(missingJSON),
		AIModel:         "mvp_heuristic_v1",
	}, nil
}

func newManifestID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "pm_" + hex.EncodeToString(b)
}
