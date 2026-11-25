// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package principal

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"
	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

type memStore struct{ entries map[string]auth.StoredToken }

func (s *memStore) Save(service, user string, tok auth.StoredToken) error {
	if s.entries == nil {
		s.entries = map[string]auth.StoredToken{}
	}
	s.entries[service+"\x00"+user] = tok
	return nil
}

func (s *memStore) Load(service, user string) (auth.StoredToken, error) {
	tok, ok := s.entries[service+"\x00"+user]
	if !ok {
		return auth.StoredToken{}, auth.ErrTokenNotFound
	}
	return tok, nil
}

func (s *memStore) Delete(service, user string) error {
	delete(s.entries, service+"\x00"+user)
	return nil
}

func (*memStore) Describe() string { return "in-memory test store" }

func testClient(t *testing.T, srv *httptest.Server) *api.AuthedClient {
	t.Helper()
	store := &memStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL, AccessToken: "login-token", Expiry: time.Now().Add(time.Hour),
	})
	client, err := api.NewAuthedClient(context.Background(), srv.URL,
		api.AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	return client
}

func stubFreshSignIn(t *testing.T, fn func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error)) *int {
	t.Helper()
	calls := 0
	old := startFreshSignIn
	startFreshSignIn = func(ctx context.Context, c *api.AuthedClient) (oauth2.TokenSource, error) {
		calls++
		return fn(ctx, c)
	}
	t.Cleanup(func() { startFreshSignIn = old })
	return &calls
}

func challenge(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("WWW-Authenticate", `Bearer error="insufficient_user_authentication", max_age=900`)
	w.WriteHeader(statusTokenRejected)
	_, _ = w.Write([]byte(`{"detail":"fresh_sign_in_required: sign in again"}`))
}

func runTestUnblock(t *testing.T, srv *httptest.Server, reason *string) (string, string, error) {
	t.Helper()
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	err := runUnblock(cmd, testClient(t, srv), srv.URL, "me", reason, false)
	return stdout.String(), stderr.String(), err
}

func TestUnblockSendsTheReasonAndSignsInAgainAfterTheChallenge(t *testing.T) {
	var bearers, reasons []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var got struct {
			Reason *string `json:"reason"`
		}
		_ = json.Unmarshal(body, &got)
		if got.Reason != nil {
			reasons = append(reasons, *got.Reason)
		}
		bearers = append(bearers, r.Header.Get("Authorization"))
		if r.Header.Get("Authorization") != "Bearer fresh-token" {
			challenge(w)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"sub":"me","removed":1}`))
	}))
	defer srv.Close()
	calls := stubFreshSignIn(t, func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error) {
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "fresh-token"}), nil
	})

	why := "the block was a mistake"
	out, errOut, err := runTestUnblock(t, srv, &why)

	if err != nil {
		t.Fatalf("unblock failed: %v %s", err, errOut)
	}
	if *calls != 1 {
		t.Errorf("fresh sign-ins = %d, want 1", *calls)
	}
	if len(bearers) != 2 || bearers[0] != "Bearer login-token" || bearers[1] != "Bearer fresh-token" {
		t.Errorf("bearers = %v", bearers)
	}
	if len(reasons) != 2 || reasons[0] != why || reasons[1] != why {
		t.Errorf("reasons = %v", reasons)
	}
	if !strings.Contains(out, "unblocked me") {
		t.Errorf("stdout = %q", out)
	}
}

func TestUnblockWithoutABrowserPrintsTheSignInLink(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { challenge(w) }))
	defer srv.Close()
	stubFreshSignIn(t, func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error) {
		return nil, &auth.NoBrowserError{URL: "https://login.test/sign-in", Err: errors.New("no display")}
	})

	why := "the block was a mistake"
	_, errOut, err := runTestUnblock(t, srv, &why)

	if err == nil {
		t.Fatal("expected an error without a browser")
	}
	want := "this unblock needs a fresh sign-in, and no browser could be opened. " +
		"Run this command on a computer where a browser can open. " +
		"Sign-in link: https://login.test/sign-in"
	if !strings.Contains(errOut, want) {
		t.Errorf("stderr = %q", errOut)
	}
	if strings.Contains(errOut, "web interface") {
		t.Errorf("message names the console: %q", errOut)
	}
}

func TestUnblockWithoutAReasonExplainsTheFlag(t *testing.T) {
	var sawReason bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		sawReason = strings.Contains(string(body), `"reason":"`)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"unblock_reason_required: needs a reason"}`))
	}))
	defer srv.Close()
	calls := stubFreshSignIn(t, func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error) {
		return nil, errors.New("must not sign in")
	})

	_, errOut, err := runTestUnblock(t, srv, nil)

	if err == nil {
		t.Fatal("expected an error without --reason")
	}
	if sawReason {
		t.Error("a reason was sent without --reason")
	}
	if *calls != 0 {
		t.Errorf("fresh sign-ins = %d, want 0", *calls)
	}
	if !strings.Contains(errOut, "Removing a block on your own login needs a reason: add --reason <text>.") {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestUnblockHasAReasonFlag(t *testing.T) {
	cmd := newUnblockCmd()
	if cmd.Flags().Lookup("reason") == nil {
		t.Fatal("unblock has no --reason flag")
	}
}
