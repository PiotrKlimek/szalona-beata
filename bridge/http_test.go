package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

type sentText struct {
	jid  types.JID
	text string
}

type fakeWA struct {
	connected, loggedIn bool
	sendErr             error
	sent                []sentText
}

func (f *fakeWA) SendText(_ context.Context, jid types.JID, text string) (string, error) {
	f.sent = append(f.sent, sentText{jid, text})
	if f.sendErr != nil {
		return "", f.sendErr
	}
	return "3EB0SENT", nil
}
func (f *fakeWA) IsConnected() bool { return f.connected }
func (f *fakeWA) IsLoggedIn() bool  { return f.loggedIn }

func do(h http.Handler, method, path, body string) (*httptest.ResponseRecorder, map[string]any) {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(method, path, strings.NewReader(body)))
	var out map[string]any
	json.Unmarshal(rec.Body.Bytes(), &out)
	return rec, out
}

func TestSendDeliversTextToGroup(t *testing.T) {
	wa := &fakeWA{}
	rec, out := do(newHandler(wa), "POST", "/send", `{"jid":"120363000000000001@g.us","text":"Podsumowanie dnia"}`)

	if rec.Code != http.StatusOK || out["id"] != "3EB0SENT" {
		t.Fatalf("status %d, body %v; want 200 with id", rec.Code, out)
	}
	want := sentText{types.NewJID("120363000000000001", types.GroupServer), "Podsumowanie dnia"}
	if len(wa.sent) != 1 || wa.sent[0] != want {
		t.Errorf("sent %+v, want [%+v]", wa.sent, want)
	}
}

func TestSendRejectsInvalidRequests(t *testing.T) {
	tests := []struct{ name, body string }{
		{"private chat jid", `{"jid":"48500100200@s.whatsapp.net","text":"hi"}`},
		{"unparseable jid", `{"jid":"not a jid@@","text":"hi"}`},
		{"empty text", `{"jid":"120363000000000001@g.us","text":""}`},
		{"malformed json", `{"jid":`},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			wa := &fakeWA{}
			rec, out := do(newHandler(wa), "POST", "/send", tt.body)

			if rec.Code != http.StatusBadRequest || out["error"] == nil {
				t.Errorf("status %d, body %v; want 400 with error", rec.Code, out)
			}
			if len(wa.sent) != 0 {
				t.Errorf("message was sent: %+v", wa.sent)
			}
		})
	}
}

func TestSendReportsWhatsAppFailure(t *testing.T) {
	wa := &fakeWA{sendErr: errors.New("websocket not connected")}
	rec, out := do(newHandler(wa), "POST", "/send", `{"jid":"120363000000000001@g.us","text":"hi"}`)

	if rec.Code != http.StatusBadGateway || out["error"] != "websocket not connected" {
		t.Errorf("status %d, body %v; want 502 with the send error", rec.Code, out)
	}
}

func TestHealthReportsConnectionState(t *testing.T) {
	rec, out := do(newHandler(&fakeWA{connected: true, loggedIn: false}), "GET", "/health", "")

	if rec.Code != http.StatusOK || out["connected"] != true || out["logged_in"] != false {
		t.Errorf("status %d, body %v; want connected=true logged_in=false", rec.Code, out)
	}
}
