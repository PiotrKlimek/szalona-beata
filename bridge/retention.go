package main

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// maxRetentionDays caps RETENTION_DAYS: larger values are typos, and very large
// ones would overflow time.Duration and wrap to a tiny (destructive) period.
const maxRetentionDays = 36500

// isExpired reports whether a message sent at ts is past the retention period (0 = never).
func isExpired(ts time.Time, retention time.Duration, now time.Time) bool {
	return retention > 0 && ts.Before(now.Add(-retention))
}

// parseRetentionDays parses RETENTION_DAYS; empty or 0 means keep messages forever.
func parseRetentionDays(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, nil
	}
	days, err := strconv.Atoi(s)
	if err != nil || days < 0 || days > maxRetentionDays {
		return 0, fmt.Errorf("RETENTION_DAYS must be a whole number of days between 0 and %d, got %q", maxRetentionDays, s)
	}
	return time.Duration(days) * 24 * time.Hour, nil
}
