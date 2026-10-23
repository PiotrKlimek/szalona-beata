package main

import (
	"context"
	"encoding/json"
	"net/http"

	"go.mau.fi/whatsmeow/types"
)

// waClient is the part of the WhatsApp connection the HTTP API needs.
type waClient interface {
	SendText(ctx context.Context, jid types.JID, text string) (string, error)
	IsConnected() bool
	IsLoggedIn() bool
}

// newHandler serves the bridge API. Sending is restricted to groups so a
// misconfigured caller cannot message private contacts.
func newHandler(wa waClient) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{
			"connected": wa.IsConnected(),
			"logged_in": wa.IsLoggedIn(),
		})
	})
	mux.HandleFunc("POST /send", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			JID  string `json:"jid"`
			Text string `json:"text"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Text == "" {
			writeError(w, http.StatusBadRequest, `expected {"jid": "...@g.us", "text": "..."}`)
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil || jid.Server != types.GroupServer {
			writeError(w, http.StatusBadRequest, "jid must be a group (@g.us)")
			return
		}
		id, err := wa.SendText(r.Context(), jid, req.Text)
		if err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"id": id})
	})
	return mux
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
