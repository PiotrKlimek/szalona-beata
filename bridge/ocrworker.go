package main

import (
	"context"
	"errors"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const (
	ocrMaxAttempts = 3
	ocrRetryDelay  = 5 * time.Minute // doubled after every failed attempt
	ocrIdlePoll    = time.Minute     // how often an idle worker looks for retries that became due
)

// ocrWorker adds the text of queued images to their messages, one image at a
// time, so a big history sync never runs more than one OCR at once.
type ocrWorker struct {
	store     *Store
	download  func(context.Context, *waE2E.ImageMessage) ([]byte, error)
	recognize func(context.Context, []byte) (ocrResult, error)
	wake      chan struct{}
	log       waLog.Logger
	now       func() time.Time
}

func newOCRWorker(store *Store, download func(context.Context, *waE2E.ImageMessage) ([]byte, error),
	recognize func(context.Context, []byte) (ocrResult, error), log waLog.Logger) *ocrWorker {
	return &ocrWorker{store: store, download: download, recognize: recognize,
		wake: make(chan struct{}, 1), log: log, now: time.Now}
}

// Enqueue queues an image message for OCR.
func (w *ocrWorker) Enqueue(chatJID, id string, ts time.Time, img *waE2E.ImageMessage) {
	data, err := proto.Marshal(img)
	if err == nil {
		err = w.store.QueueImage(chatJID, id, ts, data)
	}
	if err != nil {
		w.log.Warnf("Failed to queue image %s for OCR: %v", id, err)
		return
	}
	select {
	case w.wake <- struct{}{}:
	default: // already woken
	}
}

func (w *ocrWorker) run(ctx context.Context) {
	for {
		if w.processNext(ctx) {
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-w.wake:
		case <-time.After(ocrIdlePoll):
		}
	}
}

// processNext handles the next due image and reports whether there was one.
func (w *ocrWorker) processNext(ctx context.Context) bool {
	q, ok, err := w.store.NextImage(w.now())
	if err != nil {
		w.log.Warnf("Failed to read the OCR queue: %v", err)
		return false
	}
	if !ok {
		return false
	}
	var img waE2E.ImageMessage
	if err := proto.Unmarshal(q.Image, &img); err != nil {
		return w.finish(q, "", "unreadable queue entry: "+err.Error())
	}
	data, err := w.download(ctx, &img)
	if err != nil {
		if permanentDownloadError(err) || q.Attempts+1 >= ocrMaxAttempts {
			return w.finish(q, "", "download failed: "+err.Error())
		}
		if err := w.store.RetryImage(q.ChatJID, q.ID, w.now().Add(ocrRetryDelay<<q.Attempts)); err != nil {
			w.log.Warnf("Failed to postpone OCR of image %s: %v", q.ID, err)
			return false
		}
		return true
	}
	start := w.now()
	res, err := w.recognize(ctx, data)
	switch {
	case err != nil:
		return w.finish(q, "", err.Error())
	case res.Skipped:
		w.log.Debugf("Image %s: no text found by the fast check (%d letters in rows)", q.ID, res.Chars)
	default:
		w.log.Infof("Image %s: OCR took %s, %d characters of text", q.ID, w.now().Sub(start).Round(time.Millisecond), len([]rune(res.Text)))
	}
	return w.finish(q, ocrSuffix(res.Text), "")
}

// finish takes an image off the queue, adding suffix to its message; failure is
// the reason it gives up on the image ("" if it didn't). It reports whether the
// queue could be updated, so a broken database doesn't make the worker spin.
func (w *ocrWorker) finish(q queuedImage, suffix, failure string) bool {
	if failure != "" {
		w.log.Warnf("Skipping OCR of image %s: %s", q.ID, failure)
	}
	if err := w.store.FinishImage(q.ChatJID, q.ID, suffix); err != nil {
		w.log.Warnf("Failed to store OCR result of image %s: %v", q.ID, err)
		return false
	}
	return true
}

// permanentDownloadError reports whether retrying a download is pointless,
// e.g. the media has expired from WhatsApp's servers (old history images).
func permanentDownloadError(err error) bool {
	return errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410) ||
		errors.Is(err, whatsmeow.ErrNoURLPresent)
}
