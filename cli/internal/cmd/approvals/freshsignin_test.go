// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

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

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

const freshChallenge = `Bearer error="insufficient_user_authentication", ` +
	`error_description="A fresh sign-in is required to approve", max_age="300"`

// newFakeSignInIdP is an authorization server with a discovery
// document and a token endpoint that hands out "fresh-token".
func newFakeSignInIdP(t *testing.T) *httptest.Server {
	t.Helper()
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			_ = json.NewEncoder(w).Encode(map[string]string{
				"issuer":                        srv.URL,
				"authorization_endpoint":        srv.URL + "/auth",
				"token_endpoint":                srv.URL + "/token",
				"device_authorization_endpoint": srv.URL + "/device",
			})
		case "/token":
			_ = r.ParseForm()
			if r.Form.Get("code_verifier") == "" {
				t.Error("token request has no PKCE code_verifier")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token": "fresh-token", "token_type": "Bearer", "expires_in": 300,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// fakeSignInBrowser plays the person in the browser and counts the
// sign-ins. It checks that the link forces a new credential prompt.
func fakeSignInBrowser(t *testing.T, opened *int) func(string) error {
	return func(link string) error {
		*opened++
		u, err := url.Parse(link)
		if err != nil {
			t.Fatalf("parse sign-in link: %v", err)
		}
		q := u.Query()
		if q.Get("prompt") != "login" || q.Get("max_age") != "0" {
			t.Errorf("sign-in link must carry prompt=login and max_age=0: %s", link)
		}
		go func() {
			resp, err := http.Get(q.Get("redirect_uri") + "?code=c1&state=" + url.QueryEscape(q.Get("state")))
			if err == nil {
				_ = resp.Body.Close()
			}
		}()
		return nil
	}
}

func useBrowser(t *testing.T, open func(string) error) {
	t.Helper()
	old := openSignInBrowser
	openSignInBrowser = open
	t.Cleanup(func() { openSignInBrowser = old })
}

// newSignInTestClient stores a `meho login` token that names the fake
// identity provider. It has no refresh token: a refresh would fail.
func newSignInTestClient(t *testing.T, backplane, idp *httptest.Server) (*api.AuthedClient, *inMemoryStore) {
	t.Helper()
	store := &inMemoryStore{}
	service, user := auth.KeyForBackplane(backplane.URL)
	_ = store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplane.URL,
		Issuer:       idp.URL,
		ClientID:     "meho-cli",
		AccessToken:  "test-bearer",
		Expiry:       time.Now().Add(time.Hour),
	})
	client, err := api.NewAuthedClient(context.Background(), backplane.URL,
		api.AuthedClientOptions{Store: store, HTTPClient: backplane.Client()})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	return client, store
}

// freshSignInBackplane answers decide with the RFC 9470 challenge
// unless the bearer is one of the accepted tokens.
func freshSignInBackplane(t *testing.T, accepted map[string]bool, bearers *[]string) *httptest.Server {
	return mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, r *http.Request) {
			bearer := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			*bearers = append(*bearers, bearer)
			if !accepted[bearer] {
				w.Header().Set("WWW-Authenticate", freshChallenge)
				writeJSON(t, w, http.StatusUnauthorized, map[string]string{
					"detail": "fresh_sign_in_required: approve in the web interface, or sign in again",
				})
				return
			}
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{
				ApprovalRequestId: mustUUID(t, stubID), Decision: "approved",
			})
		},
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "approved", ""))
		},
	})
}

func TestApproveRunsAFreshSignInOnTheChallengeAndRetriesOnce(t *testing.T) {
	idp := newFakeSignInIdP(t)
	var bearers []string
	srv := freshSignInBackplane(t, map[string]bool{"fresh-token": true}, &bearers)
	defer srv.Close()
	client, store := newSignInTestClient(t, srv, idp)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	detail, err := postDecision(context.Background(), client, mustUUID(t, stubID), "approve", "")
	if err != nil {
		t.Fatalf("postDecision: %v", err)
	}
	if detail == nil || string(detail.Status) != "approved" {
		t.Fatalf("detail = %+v", detail)
	}
	if opened != 1 {
		t.Errorf("browser sign-ins = %d, want 1", opened)
	}
	if strings.Join(bearers, ",") != "test-bearer,fresh-token" {
		t.Errorf("decide bearers = %v, want the stored token then the fresh one", bearers)
	}
	// The fresh token is never written to the token store.
	service, user := auth.KeyForBackplane(srv.URL)
	stored, err := store.Load(service, user)
	if err != nil {
		t.Fatalf("store.Load: %v", err)
	}
	if stored.AccessToken != "test-bearer" {
		t.Errorf("stored access token = %q, want the old login token", stored.AccessToken)
	}
	if client.AccessToken() != "test-bearer" {
		t.Errorf("client bearer = %q, want the old login token", client.AccessToken())
	}
}

func TestApproveRetriesOnlyOnceAfterTheFreshSignIn(t *testing.T) {
	idp := newFakeSignInIdP(t)
	var bearers []string
	srv := freshSignInBackplane(t, map[string]bool{}, &bearers)
	defer srv.Close()
	client, _ := newSignInTestClient(t, srv, idp)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	_, err := postDecision(context.Background(), client, mustUUID(t, stubID), "approve", "")
	var he *httpResponseError
	if !errors.As(err, &he) || he.statusCode != http.StatusUnauthorized {
		t.Fatalf("err = %v, want the second 401", err)
	}
	if opened != 1 || len(bearers) != 2 {
		t.Errorf("sign-ins = %d, decide calls = %d; want 1 and 2", opened, len(bearers))
	}
}

func TestRejectNeverRunsAFreshSignIn(t *testing.T) {
	idp := newFakeSignInIdP(t)
	var bearers []string
	srv := freshSignInBackplane(t, map[string]bool{}, &bearers)
	defer srv.Close()
	client, _ := newSignInTestClient(t, srv, idp)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	_, err := postDecision(context.Background(), client, mustUUID(t, stubID), "reject", "")
	var he *httpResponseError
	if !errors.As(err, &he) {
		t.Fatalf("err = %v, want the 401 as an httpResponseError", err)
	}
	if opened != 0 {
		t.Errorf("browser sign-ins = %d, want 0", opened)
	}
}

func TestApproveWithoutABrowserPrintsTheLinkAndTheWebHint(t *testing.T) {
	idp := newFakeSignInIdP(t)
	var bearers []string
	srv := freshSignInBackplane(t, map[string]bool{"fresh-token": true}, &bearers)
	defer srv.Close()
	client, _ := newSignInTestClient(t, srv, idp)
	useBrowser(t, func(string) error { return errors.New("no graphical session") })

	_, err := postDecision(context.Background(), client, mustUUID(t, stubID), "approve", "")
	var nb *auth.NoBrowserError
	if !errors.As(err, &nb) {
		t.Fatalf("err = %v, want *auth.NoBrowserError", err)
	}
	se := noBrowserError(nb.URL)
	if se.Exit == 0 {
		t.Error("no-browser error must exit non-zero")
	}
	if !strings.Contains(se.Detail, "web interface") || !strings.Contains(se.Detail, idp.URL+"/auth?") {
		t.Errorf("detail = %q, want the web interface hint and the sign-in link", se.Detail)
	}
	if len(bearers) != 1 {
		t.Errorf("decide calls = %d, want 1 (no retry without a sign-in)", len(bearers))
	}
}

func TestNeedsFreshSignInReadsTheChallenge(t *testing.T) {
	for header, want := range map[string]bool{
		freshChallenge: true,
		`Bearer error=insufficient_user_authentication, max_age=300`: true,
		`Bearer error="invalid_token"`:                               false,
		"":                                                           false,
	} {
		resp := &http.Response{Header: http.Header{}}
		if header != "" {
			resp.Header.Set("WWW-Authenticate", header)
		}
		if got := needsFreshSignIn(resp); got != want {
			t.Errorf("needsFreshSignIn(%q) = %v, want %v", header, got, want)
		}
	}
	if needsFreshSignIn(nil) {
		t.Error("needsFreshSignIn(nil) = true")
	}
}
