package main

import (
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestContactNames(t *testing.T) {
	lid := types.ContactInfo{Found: true, PushName: "Ewa"}
	phone := types.ContactInfo{Found: true, PushName: "Ewa W.", FullName: "Ewa Testowa"}

	full, push := contactNames(lid, phone)
	if full != "Ewa Testowa" || push != "Ewa" {
		t.Errorf("contactNames() = %q, %q; want %q, %q", full, push, "Ewa Testowa", "Ewa")
	}
	full, push = contactNames(types.ContactInfo{FullName: "Ghost", PushName: "Ghost"}, types.ContactInfo{})
	if full != "" || push != "" {
		t.Errorf("not-found contacts: got %q, %q; want empty", full, push)
	}
}

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
