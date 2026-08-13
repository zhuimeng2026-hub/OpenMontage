// Package business resolves a business scenario key into the set of images that
// should appear in that scenario's video. The concrete implementation talks to
// the external business system; the default StubFetcher returns a configured
// static map so the rest of the pipeline (upload -> render -> poll) can be
// built and tested without the real API wired up.
package business

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
)

// ImageRef is one image that belongs to a scenario's video.
type ImageRef struct {
	// URL must be reachable by the BFF so it can download the bytes and
	// re-upload them into the upstream project's asset store.
	URL string `json:"url"`
	// Name is the safe basename used when writing the asset (e.g. "scene-01.jpg").
	Name string `json:"name"`
	// Headers are extra HTTP headers required to fetch the URL — e.g. the
	// download cookie Weiyun returns per file. Optional; the upload pipeline
	// applies them when pulling the bytes.
	Headers map[string]string `json:"-"`
}

// Fetcher turns a business scenario key (e.g. a scene/campaign id from the
// business system) into the ordered list of images for that scenario.
type Fetcher interface {
	Fetch(ctx context.Context, businessKey string) ([]ImageRef, error)
}

// StubFetcher is the default Fetcher. It serves a static map that is loaded
// from the BUSINESS_STUB_IMAGES env var (a JSON object mapping business_key to
// a list of {url,name}). Unknown keys return an error so callers fail loudly
// instead of silently producing an empty video.
type StubFetcher struct {
	Map map[string][]ImageRef
}

// NewStubFetcher parses the optional JSON map. An empty or invalid value yields
// an empty (but valid) fetcher that errors on any key.
func NewStubFetcher(jsonStr string) *StubFetcher {
	f := &StubFetcher{Map: map[string][]ImageRef{}}
	if jsonStr == "" {
		log.Println("[business] BUSINESS_STUB_IMAGES is empty — StubFetcher will reject all keys until configured.")
		return f
	}
	if err := json.Unmarshal([]byte(jsonStr), &f.Map); err != nil {
		log.Printf("[business] BUSINESS_STUB_IMAGES parse error: %v", err)
	}
	return f
}

// Fetch returns the configured images for businessKey.
func (s *StubFetcher) Fetch(_ context.Context, businessKey string) ([]ImageRef, error) {
	imgs, ok := s.Map[businessKey]
	if !ok {
		return nil, fmt.Errorf("no images configured for business key %q (set BUSINESS_STUB_IMAGES or implement a real Fetcher)", businessKey)
	}
	if len(imgs) == 0 {
		return nil, fmt.Errorf("business key %q has an empty image list", businessKey)
	}
	return imgs, nil
}
