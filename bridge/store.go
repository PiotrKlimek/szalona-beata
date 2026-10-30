package main

import (
	"database/sql"
	"fmt"
	"time"

	_ "github.com/mattn/go-sqlite3"
)

// The summarizer reads these tables directly; keep the schema in sync with it.
const schema = `
CREATE TABLE IF NOT EXISTS chats (
	jid  TEXT PRIMARY KEY,
	name TEXT
);
CREATE TABLE IF NOT EXISTS messages (
	chat_jid    TEXT NOT NULL,
	id          TEXT NOT NULL,
	sender_jid  TEXT,
	sender_name TEXT,
	ts          INTEGER NOT NULL,
	text        TEXT NOT NULL,
	from_me     INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY (chat_jid, id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages (chat_jid, ts);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages (sender_jid);
-- Images waiting for OCR. image is the marshaled waE2E.ImageMessage (download
-- URL and media key); a row is deleted once its text is added to the message.
CREATE TABLE IF NOT EXISTS ocr_queue (
	chat_jid TEXT NOT NULL,
	id       TEXT NOT NULL,
	ts       INTEGER NOT NULL,
	image    BLOB NOT NULL,
	attempts INTEGER NOT NULL DEFAULT 0,
	next_try INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY (chat_jid, id)
);
-- Voice messages waiting for transcription, like ocr_queue (audio is the
-- marshaled waE2E.AudioMessage).
CREATE TABLE IF NOT EXISTS voice_queue (
	chat_jid TEXT NOT NULL,
	id       TEXT NOT NULL,
	ts       INTEGER NOT NULL,
	audio    BLOB NOT NULL,
	attempts INTEGER NOT NULL DEFAULT 0,
	next_try INTEGER NOT NULL DEFAULT 0,
	PRIMARY KEY (chat_jid, id)
);
`

// mediaQueue is a queue of media messages whose text (recognized in an image,
// transcribed from a voice message) gets appended to the message after tag.
type mediaQueue struct {
	table, media, tag string // media: the column holding the marshaled media message
}

var (
	imageQueue  = mediaQueue{"ocr_queue", "image", ocrTag}
	voiceQueue  = mediaQueue{"voice_queue", "audio", voiceTag}
	mediaQueues = []mediaQueue{imageQueue, voiceQueue}
)

// Message is a group message as stored for the summarizer.
type Message struct {
	ChatJID    string
	ID         string
	SenderJID  string
	SenderName string
	Timestamp  time.Time
	Text       string
	FromMe     bool
}

// Store persists group chats and messages in SQLite.
type Store struct {
	db *sql.DB
}

func openStore(path string) (*Store, error) {
	db, err := sql.Open("sqlite3", "file:"+path+"?_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

// SaveChat records a group's name; an empty name never overwrites a known one.
func (s *Store) SaveChat(jid, name string) error {
	_, err := s.db.Exec(`INSERT INTO chats (jid, name) VALUES (?, ?)
		ON CONFLICT (jid) DO UPDATE SET name = COALESCE(NULLIF(excluded.name, ''), chats.name)`, jid, name)
	return err
}

// SaveMessage stores a message and reports whether it was not seen before.
func (s *Store) SaveMessage(m Message) (bool, error) {
	res, err := s.db.Exec(`INSERT OR IGNORE INTO messages
		(chat_jid, id, sender_jid, sender_name, ts, text, from_me) VALUES (?, ?, ?, ?, ?, ?, ?)`,
		m.ChatJID, m.ID, m.SenderJID, m.SenderName, m.Timestamp.Unix(), m.Text, m.FromMe)
	if err != nil {
		return false, err
	}
	n, err := res.RowsAffected()
	return n > 0, err
}

// queuedMedia is a media message waiting in a mediaQueue.
type queuedMedia struct {
	ChatJID, ID string
	Media       []byte
	Attempts    int
}

// Queue adds a media message to a queue (once).
func (s *Store) Queue(q mediaQueue, chatJID, id string, ts time.Time, media []byte) error {
	_, err := s.db.Exec(fmt.Sprintf(`INSERT OR IGNORE INTO %s (chat_jid, id, ts, %s) VALUES (?, ?, ?, ?)`, q.table, q.media),
		chatJID, id, ts.Unix(), media)
	return err
}

// Next returns the newest queued media message that is due at now: recent
// messages matter most for the next digest, and a history sync can queue hundreds.
func (s *Store) Next(q mediaQueue, now time.Time) (queuedMedia, bool, error) {
	var m queuedMedia
	err := s.db.QueryRow(fmt.Sprintf(`SELECT chat_jid, id, %s, attempts FROM %s
		WHERE next_try <= ? ORDER BY ts DESC LIMIT 1`, q.media, q.table), now.Unix()).Scan(&m.ChatJID, &m.ID, &m.Media, &m.Attempts)
	if err == sql.ErrNoRows {
		return m, false, nil
	}
	return m, err == nil, err
}

// Retry postpones a queued media message after a failed attempt.
func (s *Store) Retry(q mediaQueue, chatJID, id string, next time.Time) error {
	_, err := s.db.Exec(fmt.Sprintf(`UPDATE %s SET attempts = attempts + 1, next_try = ? WHERE chat_jid = ? AND id = ?`, q.table),
		next.Unix(), chatJID, id)
	return err
}

// Finish removes a media message from its queue and appends suffix (the text
// found in it, "" if none) to the message.
func (s *Store) Finish(q mediaQueue, chatJID, id, suffix string) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if suffix != "" { // at most once: a message can be queued again when history re-sends it
		if _, err := tx.Exec(`UPDATE messages SET text = text || ? WHERE chat_jid = ? AND id = ? AND instr(text, ?) = 0`,
			suffix, chatJID, id, q.tag); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(fmt.Sprintf(`DELETE FROM %s WHERE chat_jid = ? AND id = ?`, q.table), chatJID, id); err != nil {
		return err
	}
	return tx.Commit()
}

// HasText reports whether a stored message already has the text of its media (q's tag).
func (s *Store) HasText(q mediaQueue, chatJID, id string) bool {
	var has bool
	s.db.QueryRow(`SELECT instr(text, ?) > 0 FROM messages WHERE chat_jid = ? AND id = ?`, q.tag, chatJID, id).Scan(&has)
	return has
}

// ClearQueue empties a queue (used when OCR or transcription is turned off).
func (s *Store) ClearQueue(q mediaQueue) (int64, error) {
	res, err := s.db.Exec(fmt.Sprintf(`DELETE FROM %s`, q.table))
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

// ChatName returns the stored name of a group, or "" if unknown.
func (s *Store) ChatName(jid string) string {
	var name string
	s.db.QueryRow(`SELECT COALESCE(name, '') FROM chats WHERE jid = ?`, jid).Scan(&name)
	return name
}

// PurgeDisallowed deletes messages of groups the filter no longer allows.
func (s *Store) PurgeDisallowed(f groupFilter) (int64, error) {
	rows, err := s.db.Query(`SELECT DISTINCT m.chat_jid, COALESCE(c.name, '')
		FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid`)
	if err != nil {
		return 0, err
	}
	var disallowed []string
	for rows.Next() {
		var jid, name string
		if err := rows.Scan(&jid, &name); err != nil {
			rows.Close()
			return 0, err
		}
		if !f.Allows(jid, name) {
			disallowed = append(disallowed, jid)
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}

	var deleted int64
	for _, jid := range disallowed {
		res, err := s.db.Exec(`DELETE FROM messages WHERE chat_jid = ?`, jid)
		if err != nil {
			return deleted, err
		}
		for _, q := range mediaQueues {
			if _, err := s.db.Exec(fmt.Sprintf(`DELETE FROM %s WHERE chat_jid = ?`, q.table), jid); err != nil {
				return deleted, err
			}
		}
		n, _ := res.RowsAffected()
		deleted += n
	}
	return deleted, nil
}

// RefreshSenderNames applies the currently resolved name (address book first) to
// every stored message of each sender, so one person shows up under one name.
// Senders the resolver doesn't know keep their stored name.
func (s *Store) RefreshSenderNames(resolve func(senderJID string) string) (int64, error) {
	rows, err := s.db.Query(`SELECT DISTINCT sender_jid FROM messages`)
	if err != nil {
		return 0, err
	}
	var jids []string
	for rows.Next() {
		var jid string
		if err := rows.Scan(&jid); err != nil {
			rows.Close()
			return 0, err
		}
		jids = append(jids, jid)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}

	var updated int64
	for _, jid := range jids {
		name := resolve(jid)
		if name == "" {
			continue
		}
		res, err := s.db.Exec(`UPDATE messages SET sender_name = ? WHERE sender_jid = ? AND sender_name IS NOT ?`, name, jid, name)
		if err != nil {
			return updated, err
		}
		n, _ := res.RowsAffected()
		updated += n
	}
	return updated, nil
}

// PurgeOlderThan deletes messages (and queued media) sent before cutoff.
func (s *Store) PurgeOlderThan(cutoff time.Time) (int64, error) {
	for _, q := range mediaQueues {
		if _, err := s.db.Exec(fmt.Sprintf(`DELETE FROM %s WHERE ts < ?`, q.table), cutoff.Unix()); err != nil {
			return 0, err
		}
	}
	res, err := s.db.Exec(`DELETE FROM messages WHERE ts < ?`, cutoff.Unix())
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}
