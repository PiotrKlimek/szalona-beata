package main

import (
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

var (
	groupJID  = types.NewJID("120363000000000001", types.GroupServer)
	senderJID = types.JID{User: "48500100200", Server: types.DefaultUserServer, Device: 3}
)

func groupEvent(pushName string, msg *waE2E.Message) *events.Message {
	return &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{Chat: groupJID, Sender: senderJID, IsGroup: true},
			ID:            "3EB0ABC",
			PushName:      pushName,
			Timestamp:     time.Unix(1761200000, 0),
		},
		Message: msg,
	}
}

func noLookup(t *testing.T) nameLookup {
	return func(types.JID) (string, string) {
		t.Error("name lookup called for a message that should be skipped")
		return "", ""
	}
}

func names(full, push string) nameLookup {
	return func(types.JID) (string, string) { return full, push }
}

func TestToMessageMapsGroupTextMessage(t *testing.T) {
	evt := groupEvent("Ania", &waE2E.Message{Conversation: proto.String("cześć")})

	got, ok := toMessage(evt, names("", "")) // unknown to the contact store

	want := Message{
		ChatJID:    "120363000000000001@g.us",
		ID:         "3EB0ABC",
		SenderJID:  "48500100200@s.whatsapp.net", // device suffix stripped
		SenderName: "Ania",
		Timestamp:  time.Unix(1761200000, 0),
		Text:       "cześć",
	}
	if !ok || got != want {
		t.Errorf("toMessage() = %+v, %v; want %+v, true", got, ok, want)
	}
}

func TestToMessageSenderNameOrder(t *testing.T) {
	tests := []struct {
		name                        string
		eventPush, full, storedPush string
		want                        string
	}{
		{"address book wins over the event push name", "Ania", "Anna Kowalska", "Ania", "Anna Kowalska"},
		// the store's push name is updated asynchronously, so the event's is newer
		{"event push name wins over the stored one", "Ania (nowe)", "", "Ania", "Ania (nowe)"},
		{"stored push name when the event has none (history)", "", "", "Ania", "Ania"},
		{"nothing known", "", "", "", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := toMessage(groupEvent(tt.eventPush, &waE2E.Message{Conversation: proto.String("x")}),
				names(tt.full, tt.storedPush))
			if !ok || got.SenderName != tt.want {
				t.Errorf("SenderName = %q, want %q", got.SenderName, tt.want)
			}
		})
	}
}

func TestToMessageLooksUpTheSender(t *testing.T) {
	var lookedUp types.JID
	toMessage(groupEvent("", &waE2E.Message{Conversation: proto.String("x")}), func(jid types.JID) (string, string) {
		lookedUp = jid
		return "", ""
	})
	if lookedUp != senderJID {
		t.Errorf("looked up %v, want %v", lookedUp, senderJID)
	}
}

func TestToMessageSkipsPrivateChats(t *testing.T) {
	evt := groupEvent("Ania", &waE2E.Message{Conversation: proto.String("private")})
	evt.Info.Chat = types.NewJID("48500100200", types.DefaultUserServer)
	evt.Info.IsGroup = false

	if _, ok := toMessage(evt, noLookup(t)); ok {
		t.Error("toMessage() accepted a private chat message")
	}
}

func TestToMessageSkipsMessagesWithoutText(t *testing.T) {
	evt := groupEvent("Ania", &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Text: proto.String("👍")}})

	if _, ok := toMessage(evt, noLookup(t)); ok {
		t.Error("toMessage() accepted a reaction")
	}
}
