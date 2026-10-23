package main

import (
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// toMessage converts a WhatsApp event into a storable Message. Only group
// messages with text (or a media placeholder) are accepted. lookupName is
// consulted for the sender's name when the event carries no push name,
// which is common for history-sync messages.
func toMessage(evt *events.Message, lookupName func(types.JID) string) (Message, bool) {
	if evt.Info.Chat.Server != types.GroupServer {
		return Message{}, false
	}
	text := extractText(evt.Message)
	if text == "" {
		return Message{}, false
	}
	name := evt.Info.PushName
	if name == "" {
		name = lookupName(evt.Info.Sender)
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
