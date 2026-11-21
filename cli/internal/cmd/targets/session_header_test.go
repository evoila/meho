// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// expiredTokenStatus is the status the backplane answers for an expired
// bearer. The CLI then refreshes the token and sends the request again.
const expiredTokenStatus = 401

// TestDoAuthedRequest_SendsTheLoginSessionIDOnTheRetryToo checks that the
// hand-rolled request sends Meho-Session-Id, and sends it again on the
// retry after a 401 and a token refresh.
func TestDoAuthedRequest_SendsTheLoginSessionIDOnTheRetryToo(t *testing.T) {
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
	mux.HandleFunc("/api/v1/probe", func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get(api.SessionIDHeader))
		if len(seen) == 1 {
			w.WriteHeader(expiredTokenStatus)
			return
		}
		_, _ = w.Write([]byte(`{}`))
	})
	srv = httptest.NewServer(mux)
	defer srv.Close()

	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
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
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: srv.URL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}

	if _, err := doAuthedRequest(context.Background(), srv.URL, http.MethodGet, "/api/v1/probe", nil); err != nil {
		t.Fatalf("doAuthedRequest: %v", err)
	}
	if len(seen) != 2 || seen[0] != sessionID || seen[1] != sessionID {
		t.Errorf("session headers seen = %q; want %q on the call and on the retry", seen, sessionID)
	}
}
