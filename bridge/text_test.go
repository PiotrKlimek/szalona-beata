package main

import (
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

func TestExtractText(t *testing.T) {
	tests := []struct {
		name string
		msg  *waE2E.Message
		want string
	}{
		{"nil message", nil, ""},
		{"plain text", &waE2E.Message{Conversation: proto.String("hello")}, "hello"},
		{"extended text (reply/link)", &waE2E.Message{
			ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("see https://x.pl")},
		}, "see https://x.pl"},
		{"image with caption", &waE2E.Message{
			ImageMessage: &waE2E.ImageMessage{Caption: proto.String("sunset")},
		}, "[image] sunset"},
		{"image without caption", &waE2E.Message{ImageMessage: &waE2E.ImageMessage{}}, "[image]"},
		{"video with caption", &waE2E.Message{
			VideoMessage: &waE2E.VideoMessage{Caption: proto.String("goal!")},
		}, "[video] goal!"},
		{"voice note", &waE2E.Message{AudioMessage: &waE2E.AudioMessage{}}, "[voice note]"},
		{"document uses file name", &waE2E.Message{
			DocumentMessage: &waE2E.DocumentMessage{FileName: proto.String("invoice.pdf")},
		}, "[document] invoice.pdf"},
		{"sticker", &waE2E.Message{StickerMessage: &waE2E.StickerMessage{}}, "[sticker]"},
		{"location", &waE2E.Message{LocationMessage: &waE2E.LocationMessage{}}, "[location]"},
		{"poll", &waE2E.Message{
			PollCreationMessageV3: &waE2E.PollCreationMessage{Name: proto.String("Pizza or sushi?")},
		}, "[poll] Pizza or sushi?"},
		{"reaction is ignored", &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{Text: proto.String("👍")}}, ""},
		{"empty message", &waE2E.Message{}, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := extractText(tt.msg); got != tt.want {
				t.Errorf("extractText() = %q, want %q", got, tt.want)
			}
		})
	}
}
