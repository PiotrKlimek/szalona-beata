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

func noLookup(t *testing.T) func(types.JID) string {
	return func(types.JID) string {
		t.Error("name lookup called although push name was present")
		return ""
	}
}

func TestToMessageMapsGroupTextMessage(t *testing.T) {
	evt := groupEvent("Ania", &waE2E.Message{Conversation: proto.String("cześć")})

	got, ok := toMessage(evt, noLookup(t))

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

func TestToMessageFallsBackToContactNameWithoutPushName(t *testing.T) {
	evt := groupEvent("", &waE2E.Message{Conversation: proto.String("cześć")})
	var lookedUp types.JID

	got, ok := toMessage(evt, func(jid types.JID) string {
		lookedUp = jid
		return "Anna Kowalska"
	})

	if !ok || got.SenderName != "Anna Kowalska" {
		t.Errorf("SenderName = %q, ok=%v; want %q", got.SenderName, ok, "Anna Kowalska")
	}
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
