package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeTranscriptionAPI answers /audio/transcriptions with status and body, recording the request.
func fakeTranscriptionAPI(t *testing.T, status int, body string) (*transcriber, *http.Request, map[string]string) {
	t.Helper()
	req := &http.Request{}
	fields := map[string]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*req = *r
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Errorf("not a multipart form: %v", err)
		}
		for k, v := range r.MultipartForm.Value {
			fields[k] = v[0]
		}
		if fh := r.MultipartForm.File["file"]; len(fh) == 1 {
			f, _ := fh[0].Open()
			data, _ := io.ReadAll(f)
			fields["file"] = fh[0].Filename + ":" + string(data)
		}
		w.WriteHeader(status)
		io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	t.Setenv("LLM_API_KEY", "gsk_test")
	t.Setenv("LLM_BASE_URL", srv.URL+"/")
	t.Setenv("TRANSCRIBE_LANGUAGE", "pl")
	tr, err := newTranscriber()
	if err != nil {
		t.Fatal(err)
	}
	return tr, req, fields
}

func TestTranscribeSendsAudioAndReturnsText(t *testing.T) {
	tr, req, fields := fakeTranscriptionAPI(t, 200, `{"text": "  Jutro zbiórka o ósmej. \n"}`)

	got, err := tr.transcribe(context.Background(), []byte("OggS voice"))

	if err != nil || got != "Jutro zbiórka o ósmej." {
		t.Errorf("transcribe() = %q, %v", got, err)
	}
	if req.URL.Path != "/audio/transcriptions" || req.Header.Get("Authorization") != "Bearer gsk_test" {
		t.Errorf("request %s %s, auth %q", req.Method, req.URL.Path, req.Header.Get("Authorization"))
	}
	want := map[string]string{"file": "audio.ogg:OggS voice", "model": defaultTranscribeModel, "language": "pl",
		"response_format": "json", "temperature": "0"}
	for k, v := range want {
		if fields[k] != v {
			t.Errorf("field %s = %q; want %q", k, fields[k], v)
		}
	}
}

func TestTranscribeCapsLongTranscripts(t *testing.T) {
	tr, _, _ := fakeTranscriptionAPI(t, 200, `{"text": "`+strings.Repeat("bla ", voiceMaxChars)+`"}`)

	got, _ := tr.transcribe(context.Background(), []byte("OggS"))

	if r := []rune(got); len(r) != voiceMaxChars || r[len(r)-1] != '…' {
		t.Errorf("got %d runes; want %d ending in …", len(r), voiceMaxChars)
	}
}

func TestTranscribeErrors(t *testing.T) {
	for _, tc := range []struct {
		status    int
		temporary bool
	}{{429, true}, {503, true}, {400, false}, {401, false}, {413, false}} {
		tr, _, _ := fakeTranscriptionAPI(t, tc.status, `{"error": {"message": "nope"}}`)

		_, err := tr.transcribe(context.Background(), []byte("OggS"))

		if err == nil || !strings.Contains(err.Error(), "nope") {
			t.Errorf("%d: err = %v; want the API's message", tc.status, err)
		}
		if errors.Is(err, errTemporary) != tc.temporary {
			t.Errorf("%d: temporary = %v; want %v", tc.status, !tc.temporary, tc.temporary)
		}
	}
}

func TestTranscribeNetworkErrorIsTemporary(t *testing.T) {
	t.Setenv("LLM_API_KEY", "k")
	t.Setenv("LLM_BASE_URL", "http://127.0.0.1:1")
	tr, _ := newTranscriber()

	if _, err := tr.transcribe(context.Background(), []byte("OggS")); !errors.Is(err, errTemporary) {
		t.Errorf("err = %v; want temporary", err)
	}
}

func TestVoiceTranscriberConfig(t *testing.T) {
	for _, tc := range []struct {
		value, key string
		on, err    bool
	}{
		{"", "k", false, false},
		{"off", "k", false, false},
		{"on", "k", true, false},
		{"ON", "k", true, false},
		{"on", "", false, true}, // no API key
		{"yes", "k", false, true},
	} {
		t.Setenv("TRANSCRIBE_VOICE", tc.value)
		t.Setenv("LLM_API_KEY", tc.key)
		tr, err := voiceTranscriber()
		if (tr != nil) != tc.on || (err != nil) != tc.err {
			t.Errorf("TRANSCRIBE_VOICE=%q key=%q: on = %v, err = %v; want on = %v, err = %v",
				tc.value, tc.key, tr != nil, err, tc.on, tc.err)
		}
	}
}

func TestVoiceTranscriberDefaultsToGroq(t *testing.T) {
	t.Setenv("TRANSCRIBE_VOICE", "on")
	t.Setenv("LLM_API_KEY", "k")
	t.Setenv("LLM_BASE_URL", "")
	t.Setenv("TRANSCRIBE_MODEL", "")
	t.Setenv("TRANSCRIBE_LANGUAGE", "")
	tr, _ := voiceTranscriber()

	if tr.baseURL != "https://api.groq.com/openai/v1" || tr.model != "whisper-large-v3-turbo" || tr.language != "" {
		t.Errorf("transcriber = %+v", tr)
	}
}

func TestAudioFilename(t *testing.T) {
	for in, want := range map[string]string{
		"OggS\x00\x02":             "audio.ogg",
		"ID3\x04":                  "audio.mp3",
		"\xff\xfb\x90":             "audio.mp3",
		"\x00\x00\x00\x20ftypM4A ": "audio.m4a",
		"RIFF....WAVE":             "audio.wav",
		"???":                      "audio.ogg",
	} {
		if got := audioFilename([]byte(in)); got != want {
			t.Errorf("audioFilename(%q) = %q; want %q", in, got, want)
		}
	}
}
