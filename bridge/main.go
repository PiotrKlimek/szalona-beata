// wa-bridge links to WhatsApp as a companion device (like WhatsApp Web),
// stores group messages in a local SQLite database and exposes a small
// HTTP API (inside the compose network only) for sending messages to groups.
package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

type bridge struct {
	client    *whatsmeow.Client
	store     *Store
	filter    groupFilter
	retention time.Duration // 0 = keep messages forever
	names     *debouncer    // refreshes stored sender names off the event path
	ocr       *mediaWorker  // nil when OCR is off
	voice     *mediaWorker  // nil when voice transcription is off
	log       waLog.Logger
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "ocr" {
		langs := ocrLangs()
		if langs == "" {
			langs = "pol+eng" // OCR_LANGS=off only stops the bridge from OCR-ing incoming images
		}
		if err := runOCRCommand(os.Args[2:], langs); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "transcribe" {
		if err := runTranscribeCommand(os.Args[2:]); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	ctx := context.Background()
	dataDir := envOr("DATA_DIR", "/data")
	log := waLog.Stdout("Bridge", envOr("LOG_LEVEL", "INFO"), true)
	fatal := func(msg string, err error) {
		log.Errorf("%s: %v", msg, err)
		os.Exit(1)
	}

	// Name shown in the phone's "Linked devices" list.
	store.SetOSInfo("szalona-beata", [3]uint32{0, 1, 0})

	container, err := sqlstore.New(ctx, "sqlite3", "file:"+dataDir+"/session.db?_foreign_keys=on", waLog.Stdout("Store", "WARN", true))
	if err != nil {
		fatal("Failed to open session store", err)
	}
	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		fatal("Failed to load device", err)
	}
	messages, err := openStore(dataDir + "/messages.db")
	if err != nil {
		fatal("Failed to open messages db", err)
	}
	retention, err := parseRetentionDays(os.Getenv("RETENTION_DAYS"))
	if err != nil {
		fatal("Invalid configuration", err)
	}
	filter := parseGroupFilter(os.Getenv("WATCH_GROUPS"))
	if filter.Empty() {
		log.Warnf("WATCH_GROUPS is empty - no messages will be stored (group names are still listed)")
	}
	if n, err := messages.PurgeDisallowed(filter); err != nil {
		fatal("Failed to purge messages of unwatched groups", err)
	} else if n > 0 {
		log.Infof("Deleted %d stored messages of groups no longer in WATCH_GROUPS", n)
	}
	if retention > 0 {
		go purgeOldMessages(messages, retention, log)
	}

	b := &bridge{
		client:    whatsmeow.NewClient(device, waLog.Stdout("WA", "WARN", true)),
		store:     messages,
		filter:    filter,
		retention: retention,
		log:       log,
	}
	b.names = newDebouncer(5*time.Second, b.refreshNames)
	b.ocr = b.newOCR() // before connecting: history sync media arrives right after linking
	b.voice = b.newVoice()
	b.client.AddEventHandler(b.handleEvent)

	if b.client.Store.ID == nil {
		if err := b.login(ctx); err != nil {
			fatal("Login failed", err)
		}
	} else if err := b.client.Connect(); err != nil {
		fatal("Failed to connect", err)
	}
	for _, w := range []*mediaWorker{b.ocr, b.voice} {
		if w != nil {
			go w.run(ctx)
		}
	}

	go func() {
		addr := envOr("HTTP_ADDR", ":8080")
		log.Infof("HTTP API listening on %s", addr)
		if err := http.ListenAndServe(addr, newHandler(b)); err != nil {
			fatal("HTTP server failed", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	b.client.Disconnect()
}

// newOCR returns the OCR worker, or nil (and empties its queue) when OCR_LANGS=off.
func (b *bridge) newOCR() *mediaWorker {
	langs := ocrLangs()
	if langs == "" {
		b.clearQueue(imageQueue, "OCR is off")
		return nil
	}
	if err := checkTesseract(langs); err != nil {
		b.log.Errorf("OCR disabled: %v", err)
		return nil
	}
	b.log.Infof("OCR of images enabled (%s)", langs)
	newImage := func() mediaMessage { return &waE2E.ImageMessage{} }
	return newMediaWorker(b.store, imageQueue, "image", newImage, b.download, ocrText(langs, b.log), b.log)
}

// newVoice returns the voice transcription worker, or nil (and empties its
// queue) when TRANSCRIBE_VOICE is off or can't work.
func (b *bridge) newVoice() *mediaWorker {
	t, err := voiceTranscriber()
	if err != nil {
		b.log.Errorf("Voice transcription disabled: %v", err)
	}
	if t == nil {
		b.clearQueue(voiceQueue, "Voice transcription is off")
		return nil
	}
	b.log.Infof("Transcription of voice messages enabled (%s at %s)", t.model, t.baseURL)
	newAudio := func() mediaMessage { return &waE2E.AudioMessage{} }
	return newMediaWorker(b.store, voiceQueue, "voice message", newAudio, b.download, t.transcribe, b.log)
}

func (b *bridge) download(ctx context.Context, media mediaMessage) ([]byte, error) {
	return b.client.Download(ctx, media)
}

func (b *bridge) clearQueue(q mediaQueue, why string) {
	if n, err := b.store.ClearQueue(q); err != nil {
		b.log.Warnf("Failed to clear %s: %v", q.table, err)
	} else if n > 0 {
		b.log.Infof("%s: dropped %d queued messages", why, n)
	}
}

// purgeOldMessages deletes messages older than the retention period now and then once a day.
func purgeOldMessages(store *Store, retention time.Duration, log waLog.Logger) {
	for {
		if n, err := store.PurgeOlderThan(time.Now().Add(-retention)); err != nil {
			log.Warnf("Failed to purge old messages: %v", err)
		} else if n > 0 {
			log.Infof("Deleted %d messages older than %d days", n, int(retention.Hours()/24))
		}
		time.Sleep(24 * time.Hour)
	}
}

// login links a new device, either via QR code printed to stdout or,
// when PAIR_PHONE is set, via an 8-character pairing code.
func (b *bridge) login(ctx context.Context) error {
	qrChan, err := b.client.GetQRChannel(ctx)
	if err != nil {
		return err
	}
	if err := b.client.Connect(); err != nil {
		return err
	}
	phone := os.Getenv("PAIR_PHONE")
	pairCodeRequested := false
	for evt := range qrChan {
		switch evt.Event {
		case "code":
			if phone == "" {
				b.log.Infof("Scan this QR code in WhatsApp > Linked devices > Link a device:")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			} else if !pairCodeRequested {
				code, err := b.client.PairPhone(ctx, phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
				if err != nil {
					return err
				}
				pairCodeRequested = true
				b.log.Infof("Pairing code: %s (WhatsApp > Linked devices > Link with phone number)", code)
			}
		case "success":
			b.log.Infof("Device linked")
			return nil
		default:
			b.log.Infof("Login event: %s", evt.Event)
		}
	}
	return nil
}

func (b *bridge) handleEvent(evt any) {
	switch v := evt.(type) {
	case *events.Message:
		b.save(v, b.store.ChatName(v.Info.Chat.String()))
	case *events.HistorySync:
		b.saveHistory(v)
	case *events.Connected:
		b.log.Infof("Connected to WhatsApp")
		go b.syncGroups()
	case *events.Contact, *events.PushName, *events.AppStateSyncComplete:
		b.names.Trigger() // address book or push name changed: rename stored messages
	case *events.LoggedOut:
		b.log.Warnf("Logged out (reason %v) - remove the session volume and link again", v.Reason)
	}
}

// save stores a message if its group is watched; others are dropped unsaved.
func (b *bridge) save(evt *events.Message, chatName string) bool {
	if !b.filter.Allows(evt.Info.Chat.String(), chatName) {
		return false
	}
	if isExpired(evt.Info.Timestamp, b.retention, time.Now()) {
		return false // e.g. old history re-sent after a re-link
	}
	msg, ok := toMessage(evt, b.contactNames)
	if !ok {
		return false
	}
	isNew, err := b.store.SaveMessage(msg)
	if err != nil {
		b.log.Warnf("Failed to save message %s: %v", msg.ID, err)
	}
	if img := evt.Message.GetImageMessage(); img != nil && b.ocr != nil {
		b.ocr.Enqueue(msg.ChatJID, msg.ID, msg.Timestamp, img)
	}
	if audio := evt.Message.GetAudioMessage(); audio != nil && b.voice != nil {
		if isVoiceNote(audio) {
			b.voice.Enqueue(msg.ChatJID, msg.ID, msg.Timestamp, audio)
		} else {
			b.log.Debugf("Not transcribing audio %s (voice note: %v, %d s)", msg.ID, audio.GetPTT(), audio.GetSeconds())
		}
	}
	return isNew
}

func (b *bridge) saveHistory(v *events.HistorySync) {
	saved := 0
	for _, conv := range v.Data.GetConversations() {
		chatJID, err := types.ParseJID(conv.GetID())
		if err != nil || chatJID.Server != types.GroupServer {
			continue
		}
		b.saveChat(chatJID.String(), conv.GetName())
		chatName := b.store.ChatName(chatJID.String())
		for _, hm := range conv.GetMessages() {
			evt, err := b.client.ParseWebMessage(chatJID, hm.GetMessage())
			if err == nil && b.save(evt, chatName) {
				saved++
			}
		}
	}
	b.log.Infof("History sync (%s): saved %d group messages", v.Data.GetSyncType(), saved)
	b.names.Trigger()
}

func (b *bridge) syncGroups() {
	groups, err := b.client.GetJoinedGroups(context.Background())
	if err != nil {
		b.log.Warnf("Failed to fetch joined groups: %v", err)
		return
	}
	for _, g := range groups {
		b.saveChat(g.JID.String(), g.Name)
	}
	b.log.Infof("Synced %d groups", len(groups))
	b.names.Trigger()
}

func (b *bridge) saveChat(jid, name string) {
	if err := b.store.SaveChat(jid, name); err != nil {
		b.log.Warnf("Failed to save chat %s: %v", jid, err)
	}
}

// contactsOf returns the contact entries of a sender, also via their phone
// number when the group hides it behind a LID.
func (b *bridge) contactsOf(jid types.JID) []types.ContactInfo {
	ctx := context.Background()
	contacts := []types.ContactInfo{b.contact(ctx, jid)}
	if jid.Server == types.HiddenUserServer {
		if pn, err := b.client.Store.LIDs.GetPNForLID(ctx, jid); err == nil && !pn.IsEmpty() {
			contacts = append(contacts, b.contact(ctx, pn))
		}
	}
	return contacts
}

// contactNames implements nameLookup for incoming messages.
func (b *bridge) contactNames(jid types.JID) (fullName, pushName string) {
	return contactNames(b.contactsOf(jid)...)
}

// contactName is the best known name of a sender ("" if unknown), used to refresh stored messages.
func (b *bridge) contactName(jid types.JID) string {
	return pickName(b.contactsOf(jid)...)
}

func (b *bridge) contact(ctx context.Context, jid types.JID) types.ContactInfo {
	c, _ := b.client.Store.Contacts.GetContact(ctx, jid)
	return c
}

// refreshNames re-applies sender names to stored messages: contact names often
// arrive in a later history sync batch, and the address book name wins.
func (b *bridge) refreshNames() {
	n, err := b.store.RefreshSenderNames(func(senderJID string) string {
		jid, err := types.ParseJID(senderJID)
		if err != nil {
			return ""
		}
		return b.contactName(jid)
	})
	if err != nil {
		b.log.Warnf("Failed to refresh sender names: %v", err)
	} else if n > 0 {
		b.log.Infof("Updated sender names for %d messages", n)
	}
}

// SendText, IsConnected and IsLoggedIn implement waClient for the HTTP API.
func (b *bridge) SendText(ctx context.Context, jid types.JID, text string) (string, error) {
	resp, err := b.client.SendMessage(ctx, jid, &waE2E.Message{Conversation: proto.String(text)})
	return resp.ID, err
}

func (b *bridge) IsConnected() bool { return b.client.IsConnected() }
func (b *bridge) IsLoggedIn() bool  { return b.client.IsLoggedIn() }
