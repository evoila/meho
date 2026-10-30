// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// fakeIdP is an authorization server for the fresh sign-in tests. Its
// token endpoint checks the PKCE verifier against the challenge the
// browser saw.
type fakeIdP struct {
	srv       *httptest.Server
	challenge string
	tokenHits int
}

func newFakeIdP(t *testing.T) *fakeIdP {
	t.Helper()
	f := &fakeIdP{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/token" {
			http.NotFound(w, r)
			return
		}
		f.tokenHits++
		if err := r.ParseForm(); err != nil {
			t.Errorf("parse token form: %v", err)
		}
		if got := r.Form.Get("grant_type"); got != "authorization_code" {
			t.Errorf("grant_type = %q", got)
		}
		if got := r.Form.Get("code"); got != "the-code" {
			t.Errorf("code = %q", got)
		}
		if r.Form.Get("code_verifier") == "" {
			t.Error("token request has no code_verifier")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"access_token": "fresh-access-token",
			"token_type":   "Bearer",
			"expires_in":   300,
		})
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeIdP) doc() *DiscoveryDocument {
	return &DiscoveryDocument{
		Issuer:                f.srv.URL,
		AuthorizationEndpoint: f.srv.URL + "/auth",
		TokenEndpoint:         f.srv.URL + "/token",
	}
}

// browser plays the person: it checks the sign-in link, then follows
// the redirect back to the loopback listener with a code.
func browser(t *testing.T, f *fakeIdP, state func(string) string) func(string) error {
	return func(link string) error {
		u, err := url.Parse(link)
		if err != nil {
			t.Fatalf("parse sign-in link: %v", err)
		}
		q := u.Query()
		for key, want := range map[string]string{
			"prompt":                "login",
			"max_age":               "0",
			"response_type":         "code",
			"client_id":             "meho-cli",
			"code_challenge_method": "S256",
		} {
			if got := q.Get(key); got != want {
				t.Errorf("sign-in link %s = %q, want %q", key, got, want)
			}
		}
		f.challenge = q.Get("code_challenge")
		redirect := q.Get("redirect_uri")
		if !strings.HasPrefix(redirect, "http://127.0.0.1:") || !strings.HasSuffix(redirect, "/callback") {
			t.Errorf("redirect_uri = %q, want a 127.0.0.1 loopback callback", redirect)
		}
		go func() {
			resp, err := http.Get(redirect + "?code=the-code&state=" + url.QueryEscape(state(q.Get("state"))))
			if err == nil {
				_ = resp.Body.Close()
			}
		}()
		return nil
	}
}

func TestRunFreshSignInAsksForCredentialsAndReturnsTheToken(t *testing.T) {
	f := newFakeIdP(t)
	tok, err := RunFreshSignIn(context.Background(), f.doc(), "meho-cli", FreshSignInOptions{
		HTTPClient:  f.srv.Client(),
		OpenBrowser: browser(t, f, func(s string) string { return s }),
		Timeout:     10 * time.Second,
	})
	if err != nil {
		t.Fatalf("RunFreshSignIn: %v", err)
	}
	if tok.AccessToken != "fresh-access-token" {
		t.Errorf("access token = %q", tok.AccessToken)
	}
	if f.challenge == "" {
		t.Error("sign-in link has no code_challenge")
	}
	if f.tokenHits != 1 {
		t.Errorf("token endpoint hits = %d, want 1", f.tokenHits)
	}
}

func TestRunFreshSignInRefusesAWrongState(t *testing.T) {
	f := newFakeIdP(t)
	_, err := RunFreshSignIn(context.Background(), f.doc(), "meho-cli", FreshSignInOptions{
		HTTPClient:  f.srv.Client(),
		OpenBrowser: browser(t, f, func(string) string { return "forged" }),
		Timeout:     10 * time.Second,
	})
	if err == nil || !strings.Contains(err.Error(), "wrong state") {
		t.Fatalf("err = %v, want a wrong state error", err)
	}
	if f.tokenHits != 0 {
		t.Errorf("token endpoint hits = %d, want 0", f.tokenHits)
	}
}

func TestRunFreshSignInWithoutABrowserReturnsTheLink(t *testing.T) {
	f := newFakeIdP(t)
	_, err := RunFreshSignIn(context.Background(), f.doc(), "meho-cli", FreshSignInOptions{
		OpenBrowser: func(string) error { return errors.New("no graphical session") },
	})
	var nb *NoBrowserError
	if !errors.As(err, &nb) {
		t.Fatalf("err = %v, want *NoBrowserError", err)
	}
	if !strings.HasPrefix(nb.URL, f.srv.URL+"/auth?") || !strings.Contains(nb.URL, "prompt=login") {
		t.Errorf("link = %q", nb.URL)
	}
}

func TestRunFreshSignInTimesOut(t *testing.T) {
	f := newFakeIdP(t)
	_, err := RunFreshSignIn(context.Background(), f.doc(), "meho-cli", FreshSignInOptions{
		OpenBrowser: func(string) error { return nil },
		Timeout:     50 * time.Millisecond,
	})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v, want a deadline error", err)
	}
}

func TestRunFreshSignInNeedsAnAuthorizationEndpoint(t *testing.T) {
	_, err := RunFreshSignIn(context.Background(), &DiscoveryDocument{TokenEndpoint: "x"}, "meho-cli", FreshSignInOptions{})
	if err == nil || !strings.Contains(err.Error(), "authorization_endpoint") {
		t.Fatalf("err = %v", err)
	}
}
