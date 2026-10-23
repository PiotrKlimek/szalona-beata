package main

import (
	"testing"
	"time"
)

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
	}
	for _, tt := range tests {
		got, err := parseRetentionDays(tt.in)
		if (err != nil) != tt.wantErr || got != tt.want {
			t.Errorf("parseRetentionDays(%q) = %v, %v; want %v, err=%v", tt.in, got, err, tt.want, tt.wantErr)
		}
	}
}
