package productsvc

import (
	"encoding/json"
	"strings"
)

// Heuristic rules for MVP filename-based classification.
// Real visual models land in Phase 5+ via Agent Gateway — until then this is
// good enough to exercise the manifest rebuild path end-to-end.
//
//   filename contains "hero"      → role=hero_front, quality=0.85
//   filename contains "detail"    → role=detail,     quality=0.80
//   filename contains "lifestyle" → role=lifestyle,  quality=0.75
//   otherwise                     → role=unclassified, quality=0.50
//
// Caller-supplied overrides win over the heuristic — explicit user labels
// are higher signal than file name guesses.

type classifyResult struct {
	Role       string
	Quality    float64
	MetadataJSON string
}

// Classify runs the filename heuristic and returns (role, quality, metadata_json).
// overrideRole, when non-empty, replaces the heuristic role.
// overrideQuality, when in (0,1], replaces the heuristic quality.
func Classify(filename, overrideRole string, overrideQuality float64) (string, float64, string) {
	lower := strings.ToLower(filename)

	role := "unclassified"
	quality := 0.50
	heuristic := "none"

	switch {
	case strings.Contains(lower, "hero"):
		role = "hero_front"
		quality = 0.85
		heuristic = "filename_contains_hero"
	case strings.Contains(lower, "detail"):
		role = "detail"
		quality = 0.80
		heuristic = "filename_contains_detail"
	case strings.Contains(lower, "lifestyle"):
		role = "lifestyle"
		quality = 0.75
		heuristic = "filename_contains_lifestyle"
	}

	if overrideRole != "" {
		role = overrideRole
	}
	if overrideQuality > 0 && overrideQuality <= 1 {
		quality = overrideQuality
	}

	meta := map[string]any{
		"filename":     filename,
		"heuristic":    heuristic,
		"role_source":  roleSource(overrideRole != "", heuristic != "none"),
		"quality_set":  quality,
	}
	metaJSON, _ := json.Marshal(meta)

	return role, quality, string(metaJSON)
}

func roleSource(overrideApplied, heuristicHit bool) string {
	switch {
	case overrideApplied:
		return "user_override"
	case heuristicHit:
		return "filename_heuristic"
	default:
		return "default"
	}
}
