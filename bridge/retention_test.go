package main

import (
	"testing"
	"time"
)

func TestIsExpired(t *testing.T) {
	now := time.Unix(1_000_000, 0)
	day := 24 * time.Hour
	tests := []struct {
		name      string
		ts        time.Time
		retention time.Duration
		want      bool
	}{
		{"older than retention", now.Add(-31 * day), 30 * day, true},
		{"exactly at the cutoff is kept", now.Add(-30 * day), 30 * day, false},
		{"recent", now.Add(-time.Hour), 30 * day, false},
		{"retention off keeps everything", now.Add(-1000 * day), 0, false},
	}
	for _, tt := range tests {
		if got := isExpired(tt.ts, tt.retention, now); got != tt.want {
			t.Errorf("%s: isExpired() = %v, want %v", tt.name, got, tt.want)
		}
	}
}

func TestParseRetentionDays(t *testing.T) {
	tests := []struct {
		in      string
		want    time.Duration
		wantErr bool
	}{
		{"", 0, false},
		{"0", 0, false},
		{"720", 720 * 24 * time.Hour, false},
		{" 30 ", 30 * 24 * time.Hour, false},
		{"-1", 0, true},
		{"two years", 0, true},
		{"36500", 36500 * 24 * time.Hour, false},
		{"36501", 0, true},  // > 100 years: a typo, not a policy
		{"213504", 0, true}, // would overflow time.Duration and wrap to ~25 minutes
	}
	for _, tt := range tests {
		got, err := parseRetentionDays(tt.in)
		if (err != nil) != tt.wantErr || got != tt.want {
			t.Errorf("parseRetentionDays(%q) = %v, %v; want %v, err=%v", tt.in, got, err, tt.want, tt.wantErr)
		}
	}
}
