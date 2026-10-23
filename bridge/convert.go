package main

import (
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// toMessage converts a WhatsApp event into a storable Message. Only group
// messages with text (or a media placeholder) are accepted. The sender's name
// comes from lookupName (address book first) so one person keeps one name;
// the event's push name is the fallback for senders unknown to the store.
func toMessage(evt *events.Message, lookupName func(types.JID) string) (Message, bool) {
	if evt.Info.Chat.Server != types.GroupServer {
		return Message{}, false
	}
	text := extractText(evt.Message)
	if text == "" {
		return Message{}, false
	}
	name := lookupName(evt.Info.Sender)
	if name == "" {
		name = evt.Info.PushName
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
