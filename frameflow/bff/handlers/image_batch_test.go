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
