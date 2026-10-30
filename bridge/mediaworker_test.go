package main

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

var imageMessage = Message{
	ChatJID:   "120363000000000001@g.us",
	ID:        "IMG1",
	SenderJID: "48500100200@s.whatsapp.net",
	Timestamp: time.Unix(1761200000, 0),
	Text:      "[image] Plan lekcji",
}

type fakeOCR struct {
	downloadErr error
	extractErr  error
	text        string
	downloads   int
	recognized  [][]byte
}

func (f *fakeOCR) download(_ context.Context, media mediaMessage) ([]byte, error) {
	f.downloads++
	if f.downloadErr != nil {
		return nil, f.downloadErr
	}
	return []byte("jpeg of " + media.(*waE2E.ImageMessage).GetCaption()), nil
}

func (f *fakeOCR) extract(_ context.Context, data []byte) (string, error) {
	f.recognized = append(f.recognized, data)
	return f.text, f.extractErr
}

// newTestWorker returns a worker over a store holding imageMessage, queued for OCR.
func newTestWorker(t *testing.T, f *fakeOCR) (*mediaWorker, *Store) {
	t.Helper()
	s := newTestStore(t)
	if _, err := s.SaveMessage(imageMessage); err != nil {
		t.Fatal(err)
	}
	newImage := func() mediaMessage { return &waE2E.ImageMessage{} }
	w := newMediaWorker(s, imageQueue, "image", newImage, f.download, f.extract, waLog.Noop)
	w.Enqueue(imageMessage.ChatJID, imageMessage.ID, imageMessage.Timestamp, &waE2E.ImageMessage{Caption: proto.String("Plan lekcji")})
	return w, s
}

func messageText(t *testing.T, s *Store, id string) string {
	t.Helper()
	var text string
	if err := s.db.QueryRow(`SELECT text FROM messages WHERE id = ?`, id).Scan(&text); err != nil {
		t.Fatal(err)
	}
	return text
}

func queueLen(s *Store) int {
	var n int
	s.db.QueryRow(`SELECT COUNT(*) FROM ocr_queue`).Scan(&n)
	return n
}

func TestOCRWorkerAppendsRecognizedText(t *testing.T) {
	f := &fakeOCR{text: "Poniedziałek 8:00 matematyka"}
	w, s := newTestWorker(t, f)

	if !w.processNext(context.Background()) {
		t.Fatal("processNext() = false; want true (an image was queued)")
	}

	if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji\n[text in image]\nPoniedziałek 8:00 matematyka" {
		t.Errorf("message text = %q", got)
	}
	if len(f.recognized) != 1 || string(f.recognized[0]) != "jpeg of Plan lekcji" {
		t.Errorf("recognized %q; want the downloaded image once", f.recognized)
	}
	if n := queueLen(s); n != 0 {
		t.Errorf("queue has %d entries; want 0", n)
	}
	if w.processNext(context.Background()) {
		t.Error("second processNext() = true; want false (queue empty)")
	}
}

func TestOCRWorkerLeavesMessageAloneWithoutText(t *testing.T) {
	for name, f := range map[string]*fakeOCR{
		"no text":         {},
		"permanent error": {extractErr: errors.New("decode image: unknown format")},
	} {
		w, s := newTestWorker(t, f)

		w.processNext(context.Background())

		if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji" {
			t.Errorf("%s: message text = %q; want unchanged", name, got)
		}
		if n := queueLen(s); n != 0 {
			t.Errorf("%s: queue has %d entries; want 0", name, n)
		}
	}
}

func TestWorkerRetriesTemporaryExtractErrors(t *testing.T) {
	f := &fakeOCR{extractErr: fmt.Errorf("%w: 429 Too Many Requests", errTemporary)}
	w, s := newTestWorker(t, f)
	now := time.Unix(1761300000, 0)
	w.now = func() time.Time { return now }

	w.processNext(context.Background())
	if n := queueLen(s); n != 1 {
		t.Fatalf("queue has %d entries after a rate limit; want 1 (retry later)", n)
	}

	f.extractErr, f.text = nil, "Zbiórka o 8"
	now = now.Add(mediaRetryDelay)
	w.processNext(context.Background())

	if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji\n[text in image]\nZbiórka o 8" {
		t.Errorf("message text = %q", got)
	}
}

func TestOCRWorkerRetriesFailedDownloadsWithBackoff(t *testing.T) {
	f := &fakeOCR{downloadErr: errors.New("websocket not connected")}
	w, s := newTestWorker(t, f)
	now := time.Unix(1761300000, 0)
	w.now = func() time.Time { return now }

	for attempt := range mediaMaxAttempts - 1 {
		if !w.processNext(context.Background()) {
			t.Fatalf("attempt %d: processNext() = false; want true", attempt)
		}
		if w.processNext(context.Background()) {
			t.Fatalf("attempt %d: image retried before its delay", attempt)
		}
		now = now.Add(mediaRetryDelay << attempt)
	}
	w.processNext(context.Background()) // last attempt fails too: give up

	if f.downloads != mediaMaxAttempts {
		t.Errorf("downloads = %d; want %d", f.downloads, mediaMaxAttempts)
	}
	if n := queueLen(s); n != 0 {
		t.Errorf("queue has %d entries after %d failures; want 0", n, mediaMaxAttempts)
	}
	if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji" {
		t.Errorf("message text = %q; want unchanged", got)
	}
}

func TestOCRWorkerDropsExpiredMediaAtOnce(t *testing.T) {
	f := &fakeOCR{downloadErr: fmt.Errorf("download: %w", whatsmeow.ErrMediaDownloadFailedWith410)}
	w, s := newTestWorker(t, f)

	w.processNext(context.Background())

	if f.downloads != 1 || queueLen(s) != 0 {
		t.Errorf("downloads = %d, queue = %d; want 1, 0", f.downloads, queueLen(s))
	}
}

func TestOCRWorkerTakesNewestImagesFirst(t *testing.T) {
	f := &fakeOCR{text: "tekst"}
	w, s := newTestWorker(t, f) // IMG1 at imageMessage.Timestamp
	newer := imageMessage
	newer.ID, newer.Timestamp = "IMG2", newer.Timestamp.Add(time.Hour)
	s.SaveMessage(newer)
	w.Enqueue(newer.ChatJID, newer.ID, newer.Timestamp, &waE2E.ImageMessage{Caption: proto.String("newer")})

	w.processNext(context.Background())

	if len(f.recognized) != 1 || string(f.recognized[0]) != "jpeg of newer" {
		t.Errorf("first OCR'd %q; want the newer image", f.recognized)
	}
}

func TestOCRWorkerAppendsTextOnlyOnce(t *testing.T) {
	f := &fakeOCR{text: "Zebranie wtorek"}
	w, s := newTestWorker(t, f)
	w.processNext(context.Background())
	if !s.HasText(imageQueue, imageMessage.ChatJID, imageMessage.ID) {
		t.Fatal("HasText() = false after OCR; want true")
	}

	// History sync after a re-link delivers the same image again.
	w.Enqueue(imageMessage.ChatJID, imageMessage.ID, imageMessage.Timestamp, &waE2E.ImageMessage{Caption: proto.String("Plan lekcji")})
	w.processNext(context.Background())

	if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji\n[text in image]\nZebranie wtorek" {
		t.Errorf("message text = %q; want the text appended once", got)
	}
}

func TestEnqueueSkipsMessagesThatAlreadyHaveText(t *testing.T) {
	w, s := newTestWorker(t, &fakeOCR{text: "Zebranie wtorek"})
	w.processNext(context.Background())

	w.Enqueue(imageMessage.ChatJID, imageMessage.ID, imageMessage.Timestamp, &waE2E.ImageMessage{})

	if n := queueLen(s); n != 0 {
		t.Errorf("queue has %d entries; want 0 (the text is already there)", n)
	}
}

func TestVoiceWorkerAppendsTranscript(t *testing.T) {
	s := newTestStore(t)
	voice := Message{ChatJID: imageMessage.ChatJID, ID: "PTT1", Timestamp: imageMessage.Timestamp, Text: "[voice note]"}
	s.SaveMessage(voice)
	var downloaded *waE2E.AudioMessage
	download := func(_ context.Context, m mediaMessage) ([]byte, error) {
		downloaded = m.(*waE2E.AudioMessage)
		return []byte("OggS..."), nil
	}
	transcribe := func(context.Context, []byte) (string, error) { return "Jutro zbiórka o ósmej", nil }
	newAudio := func() mediaMessage { return &waE2E.AudioMessage{} }
	w := newMediaWorker(s, voiceQueue, "voice message", newAudio, download, transcribe, waLog.Noop)
	w.Enqueue(voice.ChatJID, voice.ID, voice.Timestamp, &waE2E.AudioMessage{PTT: proto.Bool(true), Seconds: proto.Uint32(4)})

	w.processNext(context.Background())

	if got := messageText(t, s, "PTT1"); got != "[voice note]\n[voice transcript]\nJutro zbiórka o ósmej" {
		t.Errorf("message text = %q", got)
	}
	if downloaded.GetSeconds() != 4 {
		t.Errorf("downloaded %v; want the queued audio message", downloaded)
	}
	if queueLen(s) != 0 {
		t.Error("the voice message must not land in the image queue")
	}
}

func TestTextSuffix(t *testing.T) {
	if got := textSuffix(ocrTag, ""); got != "" {
		t.Errorf("textSuffix(\"\") = %q; want \"\"", got)
	}
	if got := "[image] Plan" + textSuffix(ocrTag, "Pon 8:00"); got != "[image] Plan\n[text in image]\nPon 8:00" {
		t.Errorf("message text = %q", got)
	}
}
