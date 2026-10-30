package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	voiceTag        = "[voice transcript]" // marks a transcript in a stored message; the summarizer's prompt refers to it
	voiceMaxSeconds = 15 * 60              // longer recordings are skipped: rarely a message, and they eat the API's audio quota
	voiceMaxChars   = 3000                 // keeps one recording from eating the summarizer's transcript budget
	voiceTimeout    = 2 * time.Minute

	defaultLLMBaseURL      = "https://api.groq.com/openai/v1"
	defaultTranscribeModel = "whisper-large-v3-turbo"
)

// transcriber turns voice messages into text with an OpenAI-compatible
// /audio/transcriptions API (Groq by default, the same account as the summaries).
type transcriber struct {
	baseURL, apiKey, model, language string
	client                           *http.Client
}

// voiceTranscriber returns the transcriber configured by TRANSCRIBE_VOICE, or
// nil when transcription is off (the default: recordings go to an external API).
func voiceTranscriber() (*transcriber, error) {
	switch v := strings.ToLower(strings.TrimSpace(os.Getenv("TRANSCRIBE_VOICE"))); v {
	case "", "off":
		return nil, nil
	case "on":
	default:
		return nil, fmt.Errorf("TRANSCRIBE_VOICE must be on or off, got %q", v)
	}
	return newTranscriber()
}

func newTranscriber() (*transcriber, error) {
	key := os.Getenv("LLM_API_KEY")
	if key == "" {
		return nil, errors.New("LLM_API_KEY is not set")
	}
	return &transcriber{
		baseURL:  strings.TrimRight(envOr("LLM_BASE_URL", defaultLLMBaseURL), "/"),
		apiKey:   key,
		model:    envOr("TRANSCRIBE_MODEL", defaultTranscribeModel),
		language: strings.TrimSpace(os.Getenv("TRANSCRIBE_LANGUAGE")),
		client:   &http.Client{Timeout: voiceTimeout},
	}, nil
}

// transcribe returns the text spoken in a recording, "" if nothing was said.
// Rate limits, server and network errors wrap errTemporary.
func (t *transcriber) transcribe(ctx context.Context, audio []byte) (string, error) {
	var body bytes.Buffer
	form := multipart.NewWriter(&body)
	file, err := form.CreateFormFile("file", audioFilename(audio))
	if err != nil {
		return "", err
	}
	file.Write(audio)
	fields := map[string]string{"model": t.model, "response_format": "json", "temperature": "0"}
	if t.language != "" {
		fields["language"] = t.language
	}
	for k, v := range fields {
		form.WriteField(k, v)
	}
	if err := form.Close(); err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.baseURL+"/audio/transcriptions", &body)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+t.apiKey)
	req.Header.Set("Content-Type", form.FormDataContentType())

	resp, err := t.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("%w: transcription request: %v", errTemporary, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		err := fmt.Errorf("transcription API: %s: %s", resp.Status, strings.TrimSpace(string(data)))
		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
			err = fmt.Errorf("%w: %v", errTemporary, err)
		}
		return "", err
	}
	var out struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return "", fmt.Errorf("transcription API: unreadable response: %w", err)
	}
	return truncateRunes(strings.TrimSpace(out.Text), voiceMaxChars), nil
}

// audioFilename names an upload so the API recognizes its format; WhatsApp
// voice messages are Ogg/Opus, forwarded audio files can be anything.
func audioFilename(audio []byte) string {
	switch {
	case bytes.HasPrefix(audio, []byte("ID3")), len(audio) > 1 && audio[0] == 0xFF && audio[1]&0xE0 == 0xE0:
		return "audio.mp3"
	case len(audio) > 8 && string(audio[4:8]) == "ftyp":
		return "audio.m4a"
	case bytes.HasPrefix(audio, []byte("RIFF")):
		return "audio.wav"
	}
	return "audio.ogg"
}

// runTranscribeCommand implements "bridge transcribe FILE...": shows what the
// API makes of local recordings (they are sent to the API, as with TRANSCRIBE_VOICE=on).
func runTranscribeCommand(paths []string) error {
	t, err := newTranscriber()
	if err != nil {
		return err
	}
	for _, f := range paths {
		data, err := os.ReadFile(f)
		if err != nil {
			return err
		}
		start := time.Now()
		text, err := t.transcribe(context.Background(), data)
		took := time.Since(start).Round(time.Millisecond)
		if err != nil {
			fmt.Printf("%s: error: %v\n", f, err)
			continue
		}
		fmt.Printf("%s: %s\n  %s\n", f, took, strings.ReplaceAll(text, "\n", "\n  "))
	}
	return nil
}
