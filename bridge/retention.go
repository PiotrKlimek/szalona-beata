package main

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// parseRetentionDays parses RETENTION_DAYS; empty or 0 means keep messages forever.
func parseRetentionDays(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, nil
	}
	days, err := strconv.Atoi(s)
	if err != nil || days < 0 {
		return 0, fmt.Errorf("RETENTION_DAYS must be a whole number of days >= 0, got %q", s)
	}
	return time.Duration(days) * 24 * time.Hour, nil
}
