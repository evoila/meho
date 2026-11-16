// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// expiredTokenStatus is the status the backplane answers for an expired
// bearer. The CLI then refreshes the token and sends the request again.
const expiredTokenStatus = 401

// TestRunBroadcastWatch_SendsTheLoginSessionIDOnTheRetryToo checks that the
// feed request sends Meho-Session-Id, and sends it again on the retry after
// a 401 and a token refresh.
func TestRunBroadcastWatch_SendsTheLoginSessionIDOnTheRetryToo(t *testing.T) {
	var seen []string
	var srv *httptest.Server
	mux := http.NewServeMux()
	mux.HandleFunc("/realms/meho/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer":                        srv.URL + "/realms/meho",
			"device_authorization_endpoint": srv.URL + "/device",
			"token_endpoint":                srv.URL + "/token",
		})
	})
	mux.HandleFunc("/token", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"fresh-access","token_type":"Bearer","expires_in":300}`))
	})
	mux.HandleFunc("/api/v1/feed", func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get(api.SessionIDHeader))
		if len(seen) == 1 {
			w.WriteHeader(expiredTokenStatus)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
	})
	srv = httptest.NewServer(mux)
	defer srv.Close()

	seedXDGAndToken(t, srv.URL)
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	sessionID := auth.NewSessionID()
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "stale-access",
		RefreshToken: "stale-refresh",
		ClientID:     "meho-cli",
		Issuer:       srv.URL + "/realms/meho",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(time.Hour),
		SessionID:    sessionID,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}

	cmd, _, _ := newRunCmd(t)
	if err := runBroadcastWatch(cmd, watchOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runBroadcastWatch: %v", err)
	}
	if len(seen) != 2 || seen[0] != sessionID || seen[1] != sessionID {
		t.Errorf("session headers seen = %q; want %q on the call and on the retry", seen, sessionID)
	}
}
