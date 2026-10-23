package main

import "go.mau.fi/whatsmeow/types"

// pickName chooses a display name from the contact entries known for one
// person (e.g. their LID and phone-number entries): the name from your
// address book first, then the name they set for themselves.
func pickName(contacts ...types.ContactInfo) string {
	for _, c := range contacts {
		if c.Found && c.FullName != "" {
			return c.FullName
		}
	}
	for _, c := range contacts {
		if c.Found && c.PushName != "" {
			return c.PushName
		}
	}
	return ""
}
