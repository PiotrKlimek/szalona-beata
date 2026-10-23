package main

import (
	"database/sql"
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
`

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
		n, _ := res.RowsAffected()
		deleted += n
	}
	return deleted, nil
}

// BackfillSenderNames resolves names for senders stored without one.
// A name equal to the JID's user part (stored by early versions) counts as unknown.
func (s *Store) BackfillSenderNames(resolve func(senderJID string) string) (int64, error) {
	rows, err := s.db.Query(`SELECT DISTINCT sender_jid FROM messages
		WHERE sender_name = '' OR sender_name = substr(sender_jid, 1, instr(sender_jid, '@') - 1)`)
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
		res, err := s.db.Exec(`UPDATE messages SET sender_name = ? WHERE sender_jid = ?
			AND (sender_name = '' OR sender_name = substr(sender_jid, 1, instr(sender_jid, '@') - 1))`, name, jid)
		if err != nil {
			return updated, err
		}
		n, _ := res.RowsAffected()
		updated += n
	}
	return updated, nil
}
