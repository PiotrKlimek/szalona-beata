package main

import "go.mau.fi/whatsmeow/proto/waE2E"

// extractText returns the message text, or a short placeholder for media.
// Messages without summarizable content (reactions, protocol messages) yield "".
func extractText(m *waE2E.Message) string {
	switch {
	case m == nil:
		return ""
	case m.GetConversation() != "":
		return m.GetConversation()
	case m.GetExtendedTextMessage().GetText() != "":
		return m.GetExtendedTextMessage().GetText()
	case m.GetImageMessage() != nil:
		return withCaption("[image]", m.GetImageMessage().GetCaption())
	case m.GetVideoMessage() != nil:
		return withCaption("[video]", m.GetVideoMessage().GetCaption())
	case m.GetAudioMessage() != nil:
		return "[voice note]"
	case m.GetDocumentMessage() != nil:
		return withCaption("[document]", m.GetDocumentMessage().GetFileName())
	case m.GetStickerMessage() != nil:
		return "[sticker]"
	case m.GetLocationMessage() != nil:
		return "[location]"
	case m.GetPollCreationMessageV3() != nil:
		return withCaption("[poll]", m.GetPollCreationMessageV3().GetName())
	}
	return ""
}

func withCaption(tag, caption string) string {
	if caption == "" {
		return tag
	}
	return tag + " " + caption
}
