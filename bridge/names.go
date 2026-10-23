package main

import "go.mau.fi/whatsmeow/types"

// contactNames returns the address-book name and the self-chosen push name
// known for one person across their contact entries (e.g. LID and phone number).
func contactNames(contacts ...types.ContactInfo) (fullName, pushName string) {
	for _, c := range contacts {
		if !c.Found {
			continue
		}
		if fullName == "" {
			fullName = c.FullName
		}
		if pushName == "" {
			pushName = c.PushName
		}
	}
	return fullName, pushName
}

// pickName chooses a display name from the contact entries known for one
// person (e.g. their LID and phone-number entries): the name from your
// address book first, then the name they set for themselves.
func pickName(contacts ...types.ContactInfo) string {
	full, push := contactNames(contacts...)
	if full != "" {
		return full
	}
	return push
}
