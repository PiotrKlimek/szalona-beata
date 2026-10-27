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

func TestRefreshSenderNamesAppliesResolvedNamesToStoredMessages(t *testing.T) {
	s := newTestStore(t)
	for _, m := range []Message{
		{ID: "1", SenderJID: "111111111111111@lid", SenderName: "111111111111111"}, // legacy: id as name
		{ID: "2", SenderJID: "111111111111111@lid", SenderName: ""},
		{ID: "3", SenderJID: "222222222222222@lid", SenderName: ""},            // resolver doesn't know
		{ID: "4", SenderJID: "48500100200@s.whatsapp.net", SenderName: "Ania"}, // push name, has address book entry
		{ID: "5", SenderJID: "48500100200@s.whatsapp.net", SenderName: "Anna Kowalska"},
		{ID: "6", SenderJID: "48500100300@s.whatsapp.net", SenderName: "Bartek"}, // resolver doesn't know: keep
	} {
		m.ChatJID, m.Timestamp, m.Text = "120363000000000001@g.us", time.Unix(1761200000, 0), "x"
		s.SaveMessage(m)
	}
	resolve := func(jid string) string {
		return map[string]string{
			"111111111111111@lid":        "Ewa Testowa",
			"48500100200@s.whatsapp.net": "Anna Kowalska",
		}[jid]
	}

	updated, err := s.RefreshSenderNames(resolve)

	if err != nil || updated != 3 {
		t.Fatalf("RefreshSenderNames() = %d, %v; want 3, nil", updated, err)
	}
	var names string
	s.db.QueryRow(`SELECT GROUP_CONCAT(sender_name, '|') FROM (SELECT sender_name FROM messages ORDER BY id)`).Scan(&names)
	if names != "Ewa Testowa|Ewa Testowa||Anna Kowalska|Anna Kowalska|Bartek" {
		t.Errorf("names = %q", names)
	}
}

func TestRefreshSenderNamesIsIdempotent(t *testing.T) {
	s := newTestStore(t)
	for _, m := range []Message{
		{ID: "1", SenderJID: "48500100200@s.whatsapp.net", SenderName: "Ania"},
		{ID: "2", SenderJID: "48500100300@s.whatsapp.net", SenderName: "Bartek Nowak"}, // already the resolved name
	} {
		m.ChatJID, m.Timestamp, m.Text = "120363000000000001@g.us", time.Unix(1761200000, 0), "x"
		s.SaveMessage(m)
	}
	resolve := func(jid string) string {
		return map[string]string{
			"48500100200@s.whatsapp.net": "Anna Kowalska",
			"48500100300@s.whatsapp.net": "Bartek Nowak",
		}[jid]
	}

	first, _ := s.RefreshSenderNames(resolve)
	second, err := s.RefreshSenderNames(resolve)

	if first != 1 || second != 0 || err != nil {
		t.Errorf("updates = %d then %d (err %v); want 1 then 0", first, second, err)
	}
}

func TestRefreshSenderNamesFixesNullNames(t *testing.T) {
	s := newTestStore(t)
	// rows written outside the bridge may have NULL names
	s.db.Exec(`INSERT INTO messages (chat_jid, id, sender_jid, sender_name, ts, text) VALUES ('g@g.us', '1', 'x@lid', NULL, 1, 'x')`)

	updated, err := s.RefreshSenderNames(func(string) string { return "Ewa Testowa" })

	if err != nil || updated != 1 {
		t.Errorf("RefreshSenderNames() = %d, %v; want 1, nil", updated, err)
	}
}

func TestPurgeOlderThanDeletesOnlyOldMessages(t *testing.T) {
	s := newTestStore(t)
	s.SaveChat("120363000000000001@g.us", "Rowery")
	for _, m := range []struct {
		id string
		ts int64
	}{{"old", 1000}, {"edge", 2000}, {"new", 3000}} {
		msg := sampleMessage
		msg.ID, msg.Timestamp = m.id, time.Unix(m.ts, 0)
		s.SaveMessage(msg)
	}

	deleted, err := s.PurgeOlderThan(time.Unix(2000, 0))

	if err != nil || deleted != 1 {
		t.Fatalf("PurgeOlderThan() = %d, %v; want 1, nil", deleted, err)
	}
	var ids string
	s.db.QueryRow(`SELECT GROUP_CONCAT(id, ',') FROM (SELECT id FROM messages ORDER BY ts)`).Scan(&ids)
	if ids != "edge,new" {
		t.Errorf("remaining = %q, want %q", ids, "edge,new")
	}
	if s.ChatName("120363000000000001@g.us") != "Rowery" {
		t.Error("purge must not touch the chats table")
	}
}

func TestImageQueueFollowsMessageRetention(t *testing.T) {
	s := newTestStore(t)
	s.QueueImage("120363000000000001@g.us", "OLD", time.Unix(1000, 0), []byte("a"))
	s.QueueImage("120363000000000001@g.us", "NEW", time.Unix(3000, 0), []byte("b"))
	s.QueueImage("120363000000000003@g.us", "GONE", time.Unix(3000, 0), []byte("c"))
	s.SaveMessage(Message{ChatJID: "120363000000000003@g.us", ID: "GONE", Timestamp: time.Unix(3000, 0), Text: "[image]"})

	if _, err := s.PurgeOlderThan(time.Unix(2000, 0)); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PurgeDisallowed(parseGroupFilter("120363000000000001@g.us")); err != nil {
		t.Fatal(err)
	}

	var ids []string
	rows, _ := s.db.Query(`SELECT id FROM ocr_queue`)
	for rows.Next() {
		var id string
		rows.Scan(&id)
		ids = append(ids, id)
	}
	rows.Close()
	if len(ids) != 1 || ids[0] != "NEW" {
		t.Errorf("queued images left = %v; want [NEW]", ids)
	}
}

func TestQueueImageIgnoresDuplicates(t *testing.T) {
	s := newTestStore(t)
	s.QueueImage("120363000000000001@g.us", "IMG", time.Unix(1000, 0), []byte("a"))
	s.RetryImage("120363000000000001@g.us", "IMG", time.Unix(5000, 0))

	// The same image arrives again via history sync: it must not reset the retry state.
	if err := s.QueueImage("120363000000000001@g.us", "IMG", time.Unix(1000, 0), []byte("a")); err != nil {
		t.Fatal(err)
	}

	var n, attempts int
	s.db.QueryRow(`SELECT COUNT(*), MAX(attempts) FROM ocr_queue`).Scan(&n, &attempts)
	if n != 1 || attempts != 1 {
		t.Errorf("queue rows = %d, attempts = %d; want 1, 1", n, attempts)
	}
}
