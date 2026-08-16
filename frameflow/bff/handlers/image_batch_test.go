package handlers

import "testing"

func TestValidateImageCount(t *testing.T) {
	for _, count := range []int{5, 6, 10} {
		if err := validateImageCount(count); err != nil {
			t.Errorf("count %d rejected: %v", count, err)
		}
	}
	for _, count := range []int{0, 1, 4, 11} {
		if err := validateImageCount(count); err == nil {
			t.Errorf("count %d accepted", count)
		}
	}
}

func TestValidHTTPURLRejectsUnsafeOrEmptyShareLinks(t *testing.T) {
	for _, value := range []string{"https://share.weiyun.com/a", "http://example.test/file"} {
		if !validHTTPURL(value) {
			t.Errorf("expected URL accepted: %q", value)
		}
	}
	for _, value := range []string{"", "javascript:alert(1)", "//evil.test/file", "file:///tmp/video.mp4"} {
		if validHTTPURL(value) {
			t.Errorf("expected URL rejected: %q", value)
		}
	}
}
