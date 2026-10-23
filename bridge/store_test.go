package main

import (
	"path/filepath"
	"testing"
	"time"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := openStore(filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	t.Cleanup(func() { s.db.Close() })
	return s
}

var sampleMessage = Message{
	ChatJID:    "120363000000000001@g.us",
	ID:         "3EB0ABC",
	SenderJID:  "48500100200@s.whatsapp.net",
	SenderName: "Ania",
	Timestamp:  time.Unix(1761200000, 0),
	Text:       "Spotkanie w piątek o 18",
	FromMe:     false,
}

func TestSaveMessagePersistsAllColumns(t *testing.T) {
	s := newTestStore(t)

	isNew, err := s.SaveMessage(sampleMessage)
	if err != nil || !isNew {
		t.Fatalf("SaveMessage() = %v, %v; want true, nil", isNew, err)
	}

	var chat, id, sender, name, text string
	var ts int64
	var fromMe bool
	err = s.db.QueryRow(`SELECT chat_jid, id, sender_jid, sender_name, ts, text, from_me FROM messages`).
		Scan(&chat, &id, &sender, &name, &ts, &text, &fromMe)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if chat != "120363000000000001@g.us" || id != "3EB0ABC" || sender != "48500100200@s.whatsapp.net" ||
		name != "Ania" || ts != 1761200000 || text != "Spotkanie w piątek o 18" || fromMe {
		t.Errorf("stored row = %q %q %q %q %d %q %v", chat, id, sender, name, ts, text, fromMe)
	}
}

func TestSaveMessageIgnoresDuplicates(t *testing.T) {
	s := newTestStore(t)
	if _, err := s.SaveMessage(sampleMessage); err != nil {
		t.Fatal(err)
	}

	// The same message arrives again, e.g. live and then via history sync.
	dup := sampleMessage
	dup.Text = "edited copy"
	isNew, err := s.SaveMessage(dup)
	if err != nil || isNew {
		t.Fatalf("SaveMessage(duplicate) = %v, %v; want false, nil", isNew, err)
	}

	var count int
	var text string
	s.db.QueryRow(`SELECT COUNT(*), MAX(text) FROM messages`).Scan(&count, &text)
	if count != 1 || text != "Spotkanie w piątek o 18" {
		t.Errorf("got %d rows, text %q; want 1 row with original text", count, text)
	}
}

func TestSaveMessageSameIDInDifferentChatsIsNotDuplicate(t *testing.T) {
	s := newTestStore(t)
	other := sampleMessage
	other.ChatJID = "120363000000000002@g.us"

	s.SaveMessage(sampleMessage)
	isNew, err := s.SaveMessage(other)
	if err != nil || !isNew {
		t.Fatalf("SaveMessage(other chat) = %v, %v; want true, nil", isNew, err)
	}
}

func TestSaveChatUpdatesName(t *testing.T) {
	s := newTestStore(t)
	if err := s.SaveChat("120363000000000001@g.us", "Rowery"); err != nil {
		t.Fatal(err)
	}
	if err := s.SaveChat("120363000000000001@g.us", "Rowery Gdynia"); err != nil {
		t.Fatal(err)
	}

	var count int
	var name string
	s.db.QueryRow(`SELECT COUNT(*), MAX(name) FROM chats`).Scan(&count, &name)
	if count != 1 || name != "Rowery Gdynia" {
		t.Errorf("got %d chats, name %q; want 1 chat named %q", count, name, "Rowery Gdynia")
	}
}

func TestSaveChatKeepsNameWhenNewNameEmpty(t *testing.T) {
	s := newTestStore(t)
	s.SaveChat("120363000000000001@g.us", "Rowery")
	if err := s.SaveChat("120363000000000001@g.us", ""); err != nil {
		t.Fatal(err)
	}

	var name string
	s.db.QueryRow(`SELECT name FROM chats`).Scan(&name)
	if name != "Rowery" {
		t.Errorf("name = %q, want %q", name, "Rowery")
	}
}

func TestChatName(t *testing.T) {
	s := newTestStore(t)
	s.SaveChat("120363000000000001@g.us", "Rowery")

	if got := s.ChatName("120363000000000001@g.us"); got != "Rowery" {
		t.Errorf("ChatName(known) = %q, want %q", got, "Rowery")
	}
	if got := s.ChatName("120363000000000009@g.us"); got != "" {
		t.Errorf("ChatName(unknown) = %q, want empty", got)
	}
}

func TestPurgeDisallowedDeletesOnlyMessagesOfUnlistedGroups(t *testing.T) {
	s := newTestStore(t)
	s.SaveChat("120363000000000001@g.us", "Rowery")
	s.SaveChat("120363000000000002@g.us", "Rodzina")
	for i, chat := range []string{
		"120363000000000001@g.us", // allowed by name
		"120363000000000002@g.us", // not allowed
		"120363000000000002@g.us", // not allowed
		"120363000000000003@g.us", // allowed by jid, no chats row
	} {
		m := sampleMessage
		m.ChatJID, m.ID = chat, string(rune('a'+i))
		s.SaveMessage(m)
	}

	deleted, err := s.PurgeDisallowed(parseGroupFilter("rowery, 120363000000000003@g.us"))

	if err != nil || deleted != 2 {
		t.Fatalf("PurgeDisallowed() = %d, %v; want 2, nil", deleted, err)
	}
	var left string
	s.db.QueryRow(`SELECT GROUP_CONCAT(chat_jid, ' ') FROM (SELECT chat_jid FROM messages ORDER BY chat_jid)`).Scan(&left)
	if left != "120363000000000001@g.us 120363000000000003@g.us" {
		t.Errorf("remaining chats = %q", left)
	}
}

func TestBackfillSenderNamesFillsOnlyUnknownNames(t *testing.T) {
	s := newTestStore(t)
	for _, m := range []Message{
		{ID: "1", SenderJID: "111111111111111@lid", SenderName: "111111111111111"}, // legacy: id as name
		{ID: "2", SenderJID: "111111111111111@lid", SenderName: ""},
		{ID: "3", SenderJID: "222222222222222@lid", SenderName: ""}, // resolver doesn't know
		{ID: "4", SenderJID: "48500100200@s.whatsapp.net", SenderName: "Ania"},
	} {
		m.ChatJID, m.Timestamp, m.Text = "120363000000000001@g.us", time.Unix(1761200000, 0), "x"
		s.SaveMessage(m)
	}
	var asked []string
	resolve := func(jid string) string {
		asked = append(asked, jid)
		if jid == "111111111111111@lid" {
			return "Ewa Testowa"
		}
		return ""
	}

	updated, err := s.BackfillSenderNames(resolve)

	if err != nil || updated != 2 {
		t.Fatalf("BackfillSenderNames() = %d, %v; want 2, nil", updated, err)
	}
	var names string
	s.db.QueryRow(`SELECT GROUP_CONCAT(sender_name, '|') FROM (SELECT sender_name FROM messages ORDER BY id)`).Scan(&names)
	if names != "Ewa Testowa|Ewa Testowa||Ania" {
		t.Errorf("names = %q", names)
	}
	for _, jid := range asked {
		if jid == "48500100200@s.whatsapp.net" {
			t.Error("resolver asked about a sender that already has a name")
		}
	}
}
