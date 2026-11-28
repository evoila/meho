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
	if !strings.Contains(errOut, "needs a reason") {
		t.Errorf("stderr = %q", errOut)
	}
}

func TestUnblockPrintsTheSentenceTheServerSends(t *testing.T) {
	cases := map[string]string{
		"own": "Removing a block on your own login needs a reason: 1 to 500 characters, " +
			"with at least one character that is not a blank. Add --reason <text>.",
		"other": "A reason has 1 to 500 characters, with at least one character " +
			"that is not a blank. Leave out --reason, or fix the text.",
	}
	for name, sentence := range cases {
		t.Run(name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnprocessableEntity)
				body, _ := json.Marshal(map[string]string{"detail": "unblock_reason_required: " + sentence})
				_, _ = w.Write(body)
			}))
			defer srv.Close()
			stubFreshSignIn(t, func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error) {
				return nil, errors.New("must not sign in")
			})

			why := "x"
			_, errOut, err := runTestUnblock(t, srv, &why)

			if err == nil {
				t.Fatal("expected an error")
			}
			if !strings.Contains(errOut, sentence) {
				t.Errorf("stderr = %q, want the sentence %q", errOut, sentence)
			}
			if strings.Contains(errOut, "unblock_reason_required") {
				t.Errorf("stderr holds the code: %q", errOut)
			}
		})
	}
}

func TestUnblockSignsInWhenTheAnswerAfterARenewalIsTheChallenge(t *testing.T) {
	var bearers []string
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			_, _ = w.Write([]byte(`{"issuer":"` + srv.URL + `","token_endpoint":"` + srv.URL + `/token","device_authorization_endpoint":"` + srv.URL + `/device"}`))
			return
		case "/token":
			_, _ = w.Write([]byte(`{"access_token":"renewed-token","token_type":"Bearer","expires_in":3600}`))
			return
		}
		bearer := r.Header.Get("Authorization")
		bearers = append(bearers, bearer)
		switch bearer {
		case "Bearer login-token":
			w.WriteHeader(statusTokenRejected)
			_, _ = w.Write([]byte(`{"detail":"token expired"}`))
		case "Bearer fresh-token":
			_, _ = w.Write([]byte(`{"sub":"me","removed":1}`))
		default:
			challenge(w)
		}
	}))
	defer srv.Close()
	calls := stubFreshSignIn(t, func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error) {
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "fresh-token"}), nil
	})
	store := &memStore{}
	service, user := auth.KeyForBackplane(srv.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: srv.URL, Issuer: srv.URL, ClientID: "meho-cli",
		AccessToken: "login-token", RefreshToken: "refresh-token", Expiry: time.Now().Add(-time.Minute),
	})
	client, err := api.NewAuthedClient(context.Background(), srv.URL,
		api.AuthedClientOptions{Store: store, HTTPClient: srv.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}

	why := "the block was a mistake"
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	err = runUnblock(cmd, client, srv.URL, "me", &why, false)

	if err != nil {
		t.Fatalf("unblock failed: %v %s (bearers %v)", err, stderr.String(), bearers)
	}
	if *calls != 1 {
		t.Errorf("fresh sign-ins = %d, want 1", *calls)
	}
	if n := len(bearers); n < 3 || bearers[n-2] != "Bearer renewed-token" || bearers[n-1] != "Bearer fresh-token" {
		t.Errorf("bearers = %v, want the renewed token and then the fresh one last", bearers)
	}
	if !strings.Contains(stdout.String(), "unblocked me") {
		t.Errorf("stdout = %q", stdout.String())
	}
}

func TestUnblockHasAReasonFlag(t *testing.T) {
	cmd := newUnblockCmd()
	if cmd.Flags().Lookup("reason") == nil {
		t.Fatal("unblock has no --reason flag")
	}
}
