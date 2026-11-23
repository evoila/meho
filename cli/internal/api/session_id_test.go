// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

// seenSessionHeaders runs one GetHealth call with a stored token that
// carries sessionID, and returns the Meho-Session-Id header the server saw.
func seenSessionHeaders(t *testing.T, sessionID string, calls int) []string {
	t.Helper()
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get(SessionIDHeader))
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "test-bearer-marker",
		Expiry:       time.Now().Add(time.Hour),
		SessionID:    sessionID,
	})
	client, err := NewAuthedClient(context.Background(), srv.URL,
		AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	for i := 0; i < calls; i++ {
		if _, err := client.GetHealth(context.Background()); err != nil {
			t.Fatalf("GetHealth: %v", err)
		}
	}
	return seen
}

func TestAuthedClient_SendsTheLoginSessionIDOnEveryCall(t *testing.T) {
	id := auth.NewSessionID()
	seen := seenSessionHeaders(t, id, 2)
	if len(seen) != 2 || seen[0] != id || seen[1] != id {
		t.Errorf("session headers seen = %q; want the same id %q twice", seen, id)
	}
}

func TestAuthedClient_SendsNoSessionHeaderWithoutAStoredSessionID(t *testing.T) {
	seen := seenSessionHeaders(t, "", 1)
	if len(seen) != 1 || seen[0] != "" {
		t.Errorf("session headers seen = %q; want none", seen)
	}
}

func TestTokenBox_Refresh_KeepsTheLoginSessionID(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"fresh-access","token_type":"Bearer","expires_in":300}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	var saved auth.StoredToken
	box := &tokenBox{
		current: auth.StoredToken{
			AccessToken:  "stale-access",
			RefreshToken: "stale-refresh",
			ClientID:     "meho-cli",
			Issuer:       "https://kc.example/realms/example",
			Expiry:       time.Now().Add(-time.Hour),
			SessionID:    "11111111-2222-4333-8444-555555555555",
		},
		httpClient: srv.Client(),
		refreshDiscoverer: func(_ context.Context, _ *http.Client, _ string) (*auth.DiscoveryDocument, error) {
			return &auth.DiscoveryDocument{TokenEndpoint: srv.URL + "/token"}, nil
		},
		onRefresh: func(updated auth.StoredToken) { saved = updated },
	}
	if err := box.refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	want := "11111111-2222-4333-8444-555555555555"
	if box.sessionID() != want || saved.SessionID != want {
		t.Errorf("session id after refresh = %q (saved %q); want %q", box.sessionID(), saved.SessionID, want)
	}
}
