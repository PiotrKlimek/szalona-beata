package main

import "testing"

func TestGroupFilterAllows(t *testing.T) {
	f := parseGroupFilter(" Rowery Gdynia , 120363000000000003@g.us,, klasa 3B ")

	tests := []struct {
		name, jid, chatName string
		want                bool
	}{
		{"listed by name", "120363000000000001@g.us", "Rowery Gdynia", true},
		{"name match is case-insensitive", "120363000000000002@g.us", "Klasa 3b", true},
		{"listed by jid, name unknown", "120363000000000003@g.us", "", true},
		{"listed by jid, renamed", "120363000000000003@g.us", "New name", true},
		{"name is only a prefix of a listed one", "120363000000000004@g.us", "Rowery", false},
		{"not listed", "120363000000000005@g.us", "Rodzina", false},
		{"unknown name, jid not listed", "120363000000000006@g.us", "", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := f.Allows(tt.jid, tt.chatName); got != tt.want {
				t.Errorf("Allows(%q, %q) = %v, want %v", tt.jid, tt.chatName, got, tt.want)
			}
		})
	}
}

func TestEmptyGroupFilterAllowsNothing(t *testing.T) {
	f := parseGroupFilter("  ")

	if !f.Empty() {
		t.Error("Empty() = false for blank config")
	}
	if f.Allows("120363000000000001@g.us", "Rowery") {
		t.Error("empty filter allowed a group")
	}
}
