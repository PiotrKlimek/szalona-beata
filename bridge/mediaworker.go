package main

import (
	"context"
	"errors"
	"time"

	"go.mau.fi/whatsmeow"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const (
	mediaMaxAttempts = 3
	mediaRetryDelay  = 5 * time.Minute // doubled after every failed attempt
	mediaIdlePoll    = time.Minute     // how often an idle worker looks for retries that became due
)

// errTemporary marks extract errors that are not about the media (rate limits,
// network, a revoked API key): the worker pauses instead of giving up on it.
var errTemporary = errors.New("temporary failure")

// temporaryError is an errTemporary with how long to wait, e.g. from Retry-After (0: mediaRetryDelay).
// countAttempt: the media itself may be the cause (a timeout, a server error),
// so the failure counts against its attempts and it can't block the queue forever.
type temporaryError struct {
	msg          string
	retryAfter   time.Duration
	countAttempt bool
}

func (e *temporaryError) Error() string { return e.msg }
func (e *temporaryError) Unwrap() error { return errTemporary }

// mediaMessage is a WhatsApp media message (image, audio) that can be queued and downloaded.
type mediaMessage interface {
	proto.Message
	whatsmeow.DownloadableMessage
}

// mediaWorker adds the text found in queued media (text in images, transcripts
// of voice messages) to their messages, one at a time, so a big history sync
// never runs more than one OCR or transcription at once.
type mediaWorker struct {
	store    *Store
	queue    mediaQueue
	kind     string // "image", "voice message": for logs
	newMedia func() mediaMessage
	download func(context.Context, mediaMessage) ([]byte, error)
	extract  func(context.Context, []byte) (string, error) // the text in the media, "" if none
	wake     chan struct{}
	log      waLog.Logger
	now      func() time.Time
	paused   time.Time // after a temporary extract error nothing is processed until then
}

func newMediaWorker(store *Store, queue mediaQueue, kind string, newMedia func() mediaMessage,
	download func(context.Context, mediaMessage) ([]byte, error),
	extract func(context.Context, []byte) (string, error), log waLog.Logger) *mediaWorker {
	return &mediaWorker{store: store, queue: queue, kind: kind, newMedia: newMedia, download: download,
		extract: extract, wake: make(chan struct{}, 1), log: log, now: time.Now}
}

// Enqueue queues a media message, unless its message already has the text.
// Media stored before (e.g. by a bridge without OCR) is queued too when
// history sends it again after a re-link.
func (w *mediaWorker) Enqueue(chatJID, id string, ts time.Time, media mediaMessage) {
	if w.store.HasText(w.queue, chatJID, id) {
		return
	}
	data, err := proto.Marshal(media)
	if err == nil {
		err = w.store.Queue(w.queue, chatJID, id, ts, data)
	}
	if err != nil {
		w.log.Warnf("Failed to queue %s %s: %v", w.kind, id, err)
		return
	}
	select {
	case w.wake <- struct{}{}:
	default: // already woken
	}
}

func (w *mediaWorker) run(ctx context.Context) {
	for {
		if w.processNext(ctx) {
			continue
		}
		select {
		case <-ctx.Done():
			return
		case <-w.wake:
		case <-time.After(mediaIdlePoll):
		}
	}
}

// processNext handles the next due media message and reports whether there was one.
func (w *mediaWorker) processNext(ctx context.Context) bool {
	if w.now().Before(w.paused) {
		return false
	}
	q, ok, err := w.store.Next(w.queue, w.now())
	if err != nil {
		w.log.Warnf("Failed to read the %s queue: %v", w.kind, err)
		return false
	}
	if !ok {
		return false
	}
	media := w.newMedia()
	if err := proto.Unmarshal(q.Media, media); err != nil {
		return w.finish(q, "", "unreadable queue entry: "+err.Error())
	}
	data, err := w.download(ctx, media)
	if err != nil {
		if permanentDownloadError(err) {
			return w.finish(q, "", "download failed: "+err.Error())
		}
		return w.retry(q, "download failed: "+err.Error())
	}
	start := w.now()
	text, err := w.extract(ctx, data)
	switch {
	case errors.Is(err, errTemporary):
		// It would most likely hit the next message too: pause the whole queue.
		// Rate limits and a rejected key don't use up the message's attempts.
		delay := mediaRetryDelay
		var te *temporaryError
		if errors.As(err, &te) && te.retryAfter > 0 {
			delay = te.retryAfter
		}
		w.paused = w.now().Add(delay)
		w.log.Warnf("Pausing %s processing for %s: %v", w.kind, delay, err)
		if te != nil && te.countAttempt {
			return w.retry(q, err.Error())
		}
		return false
	case err != nil:
		return w.finish(q, "", err.Error())
	case text == "":
		w.log.Debugf("Read %s %s in %s: no text", w.kind, q.ID, w.now().Sub(start).Round(time.Millisecond))
	default:
		w.log.Infof("Read %s %s in %s: %d characters of text", w.kind, q.ID, w.now().Sub(start).Round(time.Millisecond), len([]rune(text)))
	}
	return w.finish(q, textSuffix(w.queue.tag, text), "")
}

// retry postpones a media message after a failed attempt, giving up after mediaMaxAttempts.
func (w *mediaWorker) retry(q queuedMedia, failure string) bool {
	if q.Attempts+1 >= mediaMaxAttempts {
		return w.finish(q, "", failure)
	}
	delay := mediaRetryDelay << q.Attempts
	if err := w.store.Retry(w.queue, q.ChatJID, q.ID, w.now().Add(delay)); err != nil {
		w.log.Warnf("Failed to postpone %s %s: %v", w.kind, q.ID, err)
		return false
	}
	w.log.Infof("Retrying %s %s in %s: %s", w.kind, q.ID, delay, failure)
	return true
}

// finish takes a media message off the queue, adding suffix to its message; failure
// is the reason it gives up on it ("" if it didn't). It reports whether the
// queue could be updated, so a broken database doesn't make the worker spin.
func (w *mediaWorker) finish(q queuedMedia, suffix, failure string) bool {
	if failure != "" {
		w.log.Warnf("Skipping %s %s: %s", w.kind, q.ID, failure)
	}
	if err := w.store.Finish(w.queue, q.ChatJID, q.ID, suffix); err != nil {
		w.log.Warnf("Failed to store the text of %s %s: %v", w.kind, q.ID, err)
		return false
	}
	return true
}

// textSuffix is what gets appended to a media message's text for the text found in it.
func textSuffix(tag, text string) string {
	if text == "" {
		return ""
	}
	return "\n" + tag + "\n" + text
}

// permanentDownloadError reports whether retrying a download is pointless,
// e.g. the media has expired from WhatsApp's servers (old history images).
func permanentDownloadError(err error) bool {
	return errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith403) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith404) ||
		errors.Is(err, whatsmeow.ErrMediaDownloadFailedWith410) ||
		errors.Is(err, whatsmeow.ErrNoURLPresent)
}
