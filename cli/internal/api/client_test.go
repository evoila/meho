// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/auth"
)

// inMemoryStore satisfies auth.TokenStore for tests without
// touching the OS keyring or the file system.
type inMemoryStore struct {
	entries map[string]auth.StoredToken
}

func (s *inMemoryStore) key(service, user string) string {
	return service + "\x00" + user
}

func (s *inMemoryStore) Save(service, user string, tok auth.StoredToken) error {
	if s.entries == nil {
		s.entries = map[string]auth.StoredToken{}
	}
	s.entries[s.key(service, user)] = tok
	return nil
}

func (s *inMemoryStore) Load(service, user string) (auth.StoredToken, error) {
	tok, ok := s.entries[s.key(service, user)]
	if !ok {
		return auth.StoredToken{}, auth.ErrTokenNotFound
	}
	return tok, nil
}

func (s *inMemoryStore) Delete(service, user string) error {
	delete(s.entries, s.key(service, user))
	return nil
}

func (inMemoryStore) Describe() string { return "in-memory test store" }

// TestNewAuthedClient_NoStoredToken pins the IsTokenNotFound seam:
// missing credentials surface a sentinel callers can errors.Is
// against without importing internal/auth.
func TestNewAuthedClient_NoStoredToken(t *testing.T) {
	store := &inMemoryStore{}
	_, err := NewAuthedClient(context.Background(), "https://meho.example",
		AuthedClientOptions{Store: store})
	if err == nil {
		t.Fatal("expected error")
	}
	if !IsTokenNotFound(err) {
		t.Errorf("expected IsTokenNotFound, got %v", err)
	}
}

// TestAuthedClient_GetHealth_HappyPath confirms the bearer header
// gets stamped on every outbound request via the editor.
func TestAuthedClient_GetHealth_HappyPath(t *testing.T) {
	var seenAuth string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, r *http.Request) {
		seenAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
            "operator": {"sub": "x", "email": null, "name": null},
            "vault": {"reachable": true, "read_ok": true, "detail": null},
            "db": {"migrated": true}
        }`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "test-bearer-marker",
		Expiry:       time.Now().Add(time.Hour),
	})
	client, err := NewAuthedClient(context.Background(), srv.URL,
		AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	resp, err := client.GetHealth(context.Background())
	if err != nil {
		t.Fatalf("GetHealth: %v", err)
	}
	if resp.StatusCode() != http.StatusOK {
		t.Errorf("expected 200, got %d", resp.StatusCode())
	}
	if seenAuth != "Bearer test-bearer-marker" {
		t.Errorf("authorization header: %q", seenAuth)
	}
}

// TestAuthedClient_GetHealth_NoRefreshToken_Returns401_AndNoRefreshSentinel
// confirms the 401 path: when no refresh_token is present, the
// CLI surfaces IsNoRefreshToken so the cobra command can map onto
// output.AuthExpired with a `meho login` hint.
func TestAuthedClient_GetHealth_NoRefreshToken_Returns401_AndNoRefreshSentinel(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":"unauthorized"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "stale-bearer",
		// RefreshToken intentionally empty.
		Expiry: time.Now().Add(time.Hour),
	})
	client, err := NewAuthedClient(context.Background(), srv.URL,
		AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	_, err = client.GetHealth(context.Background())
	if err == nil {
		t.Fatal("expected error from 401 + no-refresh-token path")
	}
	if !IsNoRefreshToken(err) {
		t.Errorf("expected IsNoRefreshToken, got %v", err)
	}
}

// TestAuthedClient_GetHealth_RefreshDiscoveryFailure confirms the
// 401 + present-refresh_token path: when discovery fails (e.g.
// issuer URL unreachable), the refresh attempt propagates the
// underlying error rather than masquerading as auth_expired.
func TestAuthedClient_GetHealth_RefreshDiscoveryFailure(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL,
		AccessToken:  "stale-bearer",
		RefreshToken: "stale-refresh",
		Issuer:       "https://kc.invalid",
		ClientID:     "meho-cli",
		Expiry:       time.Now().Add(time.Hour),
	})

	discoErr := errors.New("simulated discovery failure")
	client, err := NewAuthedClient(context.Background(), srv.URL,
		AuthedClientOptions{
			Store:      store,
			HTTPClient: srv.Client(),
			RefreshDiscoverer: func(_ context.Context, _ *http.Client, _ string) (*auth.DiscoveryDocument, error) {
				return nil, discoErr
			},
		})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	_, err = client.GetHealth(context.Background())
	if err == nil {
		t.Fatal("expected discovery failure to propagate")
	}
	if !errors.Is(err, discoErr) {
		t.Errorf("expected wrapped %v, got %v", discoErr, err)
	}
}
