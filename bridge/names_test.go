package main

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestPickName(t *testing.T) {
	missing := types.ContactInfo{}
	lidContact := types.ContactInfo{Found: true, PushName: "Ewa"}
	phoneContact := types.ContactInfo{Found: true, PushName: "Ewa", FullName: "Ewa Testowa"}

	tests := []struct {
		name     string
		contacts []types.ContactInfo
		want     string
	}{
		{"address book name wins over push name", []types.ContactInfo{lidContact, phoneContact}, "Ewa Testowa"},
		{"push name when nobody has a full name", []types.ContactInfo{missing, lidContact}, "Ewa"},
		{"ignores not-found contacts with stale fields", []types.ContactInfo{{FullName: "Ghost"}, lidContact}, "Ewa"},
		{"unknown sender", []types.ContactInfo{missing, missing}, ""},
		{"no contacts", nil, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := pickName(tt.contacts...); got != tt.want {
				t.Errorf("pickName() = %q, want %q", got, tt.want)
			}
		})
	}
}
