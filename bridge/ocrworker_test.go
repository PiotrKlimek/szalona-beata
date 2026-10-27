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
	result      ocrResult
	downloads   int
	recognized  [][]byte
}

func (f *fakeOCR) download(_ context.Context, img *waE2E.ImageMessage) ([]byte, error) {
	f.downloads++
	if f.downloadErr != nil {
		return nil, f.downloadErr
	}
	return []byte("jpeg of " + img.GetCaption()), nil
}

func (f *fakeOCR) recognize(_ context.Context, data []byte) (ocrResult, error) {
	f.recognized = append(f.recognized, data)
	return f.result, nil
}

// newTestWorker returns a worker over a store holding imageMessage, queued for OCR.
func newTestWorker(t *testing.T, f *fakeOCR) (*ocrWorker, *Store) {
	t.Helper()
	s := newTestStore(t)
	if _, err := s.SaveMessage(imageMessage); err != nil {
		t.Fatal(err)
	}
	w := newOCRWorker(s, f.download, f.recognize, waLog.Noop)
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
	f := &fakeOCR{result: ocrResult{Chars: 40, Text: "Poniedziałek 8:00 matematyka"}}
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
	for name, res := range map[string]ocrResult{
		"skipped by the fast check": {Chars: 2, Skipped: true},
		"nothing confident":         {Chars: 30},
	} {
		w, s := newTestWorker(t, &fakeOCR{result: res})

		w.processNext(context.Background())

		if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji" {
			t.Errorf("%s: message text = %q; want unchanged", name, got)
		}
		if n := queueLen(s); n != 0 {
			t.Errorf("%s: queue has %d entries; want 0", name, n)
		}
	}
}

func TestOCRWorkerRetriesFailedDownloadsWithBackoff(t *testing.T) {
	f := &fakeOCR{downloadErr: errors.New("websocket not connected")}
	w, s := newTestWorker(t, f)
	now := time.Unix(1761300000, 0)
	w.now = func() time.Time { return now }

	for attempt := range ocrMaxAttempts - 1 {
		if !w.processNext(context.Background()) {
			t.Fatalf("attempt %d: processNext() = false; want true", attempt)
		}
		if w.processNext(context.Background()) {
			t.Fatalf("attempt %d: image retried before its delay", attempt)
		}
		now = now.Add(ocrRetryDelay << attempt)
	}
	w.processNext(context.Background()) // last attempt fails too: give up

	if f.downloads != ocrMaxAttempts {
		t.Errorf("downloads = %d; want %d", f.downloads, ocrMaxAttempts)
	}
	if n := queueLen(s); n != 0 {
		t.Errorf("queue has %d entries after %d failures; want 0", n, ocrMaxAttempts)
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
	f := &fakeOCR{result: ocrResult{Chars: 40, Text: "tekst"}}
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
	f := &fakeOCR{result: ocrResult{Chars: 40, Text: "Zebranie wtorek"}}
	w, s := newTestWorker(t, f)
	w.processNext(context.Background())
	if !s.HasOCRText(imageMessage.ChatJID, imageMessage.ID) {
		t.Fatal("HasOCRText() = false after OCR; want true")
	}

	// History sync after a re-link delivers the same image again.
	w.Enqueue(imageMessage.ChatJID, imageMessage.ID, imageMessage.Timestamp, &waE2E.ImageMessage{Caption: proto.String("Plan lekcji")})
	w.processNext(context.Background())

	if got := messageText(t, s, "IMG1"); got != "[image] Plan lekcji\n[text in image]\nZebranie wtorek" {
		t.Errorf("message text = %q; want the text appended once", got)
	}
}
