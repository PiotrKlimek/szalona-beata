package main

import (
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// nameLookup returns the address-book name and the stored push name of a sender.
type nameLookup func(types.JID) (fullName, pushName string)

// toMessage converts a WhatsApp event into a storable Message. Only group
// messages with text (or a media placeholder) are accepted. Sender name order:
// address book (so one person keeps one name), then the event's push name
// (newer than the store's, which whatsmeow updates asynchronously), then the
// stored push name (history messages often carry none).
func toMessage(evt *events.Message, lookupName nameLookup) (Message, bool) {
	if evt.Info.Chat.Server != types.GroupServer {
		return Message{}, false
	}
	text := extractText(evt.Message)
	if text == "" {
		return Message{}, false
	}
	name, storedPush := lookupName(evt.Info.Sender)
	if name == "" {
		name = evt.Info.PushName
	}
	if name == "" {
		name = storedPush
	}
	return Message{
		ChatJID:    evt.Info.Chat.String(),
		ID:         evt.Info.ID,
		SenderJID:  evt.Info.Sender.ToNonAD().String(),
		SenderName: name,
		Timestamp:  evt.Info.Timestamp,
		Text:       text,
		FromMe:     evt.Info.IsFromMe,
	}, true
}
