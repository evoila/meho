// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// freshChallenge is the 401 challenge the backplane sends (RFC 9470 §3).
const freshChallenge = `Bearer error="insufficient_user_authentication", max_age=300`

// newFakeSignInIdP is an authorization server with a discovery
// document and a token endpoint that hands out "fresh-token".
func newFakeSignInIdP(t *testing.T) *httptest.Server {
	t.Helper()
	return newFakeSignInIdPWithExpiry(t, 300)
}

// newFakeSignInIdPWithExpiry is newFakeSignInIdP with the lifetime of
// "fresh-token". A refresh hands out "renewed-token".
func newFakeSignInIdPWithExpiry(t *testing.T, expiresIn int) *httptest.Server {
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
			if r.Form.Get("grant_type") == "refresh_token" {
				// The in-memory renewal of the sign-in token.
				_ = json.NewEncoder(w).Encode(map[string]any{
					"access_token": "renewed-token", "token_type": "Bearer", "expires_in": 300,
				})
				return
			}
			if r.Form.Get("code_verifier") == "" {
				t.Error("token request has no PKCE code_verifier")
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token": "fresh-token", "token_type": "Bearer", "expires_in": expiresIn,
				"refresh_token": "fresh-refresh",
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// fakeSignInBrowser plays the person in the browser and counts the
// sign-ins. It checks that the link forces a new credential prompt and
// names the approval client.
func fakeSignInBrowser(t *testing.T, opened *int) func(string) error {
	return fakeSignInBrowserFor(t, opened, approveClient)
}

func fakeSignInBrowserFor(t *testing.T, opened *int, wantClient string) func(string) error {
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
		if q.Get("client_id") != wantClient {
			t.Errorf("sign-in client = %q, want %q", q.Get("client_id"), wantClient)
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

// stubSignIn stands in for the browser sign-in of approvals in tests
// that are not about it. Each sign-in gives the token "sign-in-<n>". It
// returns the number of sign-ins so far.
func stubSignIn(t *testing.T) *int {
	t.Helper()
	count := 0
	old := startApprovalSignIn
	startApprovalSignIn = func(context.Context, *api.AuthedClient) (oauth2.TokenSource, error) {
		count++
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: fmt.Sprintf("sign-in-%d", count)}), nil
	}
	t.Cleanup(func() { startApprovalSignIn = old })
	return &count
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

// approveClient is the approval client the test backplane names.
const approveClient = "meho-cli-approve"

func authConfigRoute(t *testing.T) mockHandler {
	return func(w http.ResponseWriter, _ *http.Request) {
		c := approveClient
		writeJSON(t, w, http.StatusOK, api.AuthConfigResponse{
			KeycloakIssuer: "unused", Audience: "meho-backplane", CliClientId: "meho-cli", CliApproveClientId: &c,
		})
	}
}

// freshSignInBackplane answers decide with the RFC 9470 challenge
// unless the bearer is one of the accepted tokens.
func freshSignInBackplane(t *testing.T, accepted map[string]bool, bearers *[]string) *httptest.Server {
	return mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/auth-config": authConfigRoute(t),
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

func TestApproveUsesTheApprovalSignInNotTheStoredLogin(t *testing.T) {
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
	// The stored login never approves, so it is not even tried (#5515).
	if strings.Join(bearers, ",") != "fresh-token" {
		t.Errorf("decide bearers = %v, want only the fresh one", bearers)
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

func TestApproveSignsInAgainOnlyOnceAfterARefusal(t *testing.T) {
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
	// One sign-in, refused; one new sign-in, refused again; then it stops.
	if opened != 2 || len(bearers) != 2 {
		t.Errorf("sign-ins = %d, decide calls = %d; want 2 and 2", opened, len(bearers))
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
	if len(bearers) != 0 {
		t.Errorf("decide calls = %d, want 0 (no approve without a sign-in)", len(bearers))
	}
}

func TestNeedsFreshSignInReadsTheChallenge(t *testing.T) {
	for header, want := range map[string]bool{
		freshChallenge: true,
		`Bearer error=insufficient_user_authentication, max_age="300"`: true,
		`Bearer error="invalid_token"`:                                 false,
		"":                                                             false,
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

func TestApproveRenewsTheSignInTokenInMemory(t *testing.T) {
	// The token lives 5 seconds, less than the renewal margin, so the
	// first use renews it with its refresh token: no second sign-in.
	idp := newFakeSignInIdPWithExpiry(t, 5)
	var bearers []string
	srv := freshSignInBackplane(t, map[string]bool{"renewed-token": true}, &bearers)
	defer srv.Close()
	client, _ := newSignInTestClient(t, srv, idp)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	if _, err := postDecision(context.Background(), client, mustUUID(t, stubID), "approve", ""); err != nil {
		t.Fatalf("postDecision: %v", err)
	}
	if opened != 1 || strings.Join(bearers, ",") != "renewed-token" {
		t.Errorf("sign-ins = %d, bearers = %v; want 1 and the renewed token", opened, bearers)
	}
}

func TestApproveUsesTheLoginClientOnAnOlderMEHO(t *testing.T) {
	// A MEHO from before the approval client does not send
	// cli_approve_client_id; the sign-in then uses the login client.
	idp := newFakeSignInIdP(t)
	var bearers []string
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/auth-config": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, map[string]string{
				"keycloak_issuer": "unused", "audience": "meho-backplane", "cli_client_id": "meho-cli",
			})
		},
		"POST /api/v1/approvals/" + stubID + "/decide": func(w http.ResponseWriter, r *http.Request) {
			bearers = append(bearers, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{ApprovalRequestId: mustUUID(t, stubID), Decision: "approved"})
		},
		"GET /api/v1/approvals/" + stubID: func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, newApprovalView(t, "approved", ""))
		},
	})
	defer srv.Close()
	client, _ := newSignInTestClient(t, srv, idp)
	opened := 0
	useBrowser(t, fakeSignInBrowserFor(t, &opened, "meho-cli"))

	if _, err := postDecision(context.Background(), client, mustUUID(t, stubID), "approve", ""); err != nil {
		t.Fatalf("postDecision: %v", err)
	}
	if opened != 1 || strings.Join(bearers, ",") != "fresh-token" {
		t.Errorf("sign-ins = %d, bearers = %v", opened, bearers)
	}
}

func TestApproveRefusesWhenMEHOAcceptsNoCommandLineApproval(t *testing.T) {
	idp := newFakeSignInIdP(t)
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/auth-config": func(w http.ResponseWriter, _ *http.Request) {
			empty := ""
			writeJSON(t, w, http.StatusOK, api.AuthConfigResponse{
				KeycloakIssuer: "unused", Audience: "meho-backplane", CliClientId: "meho-cli", CliApproveClientId: &empty,
			})
		},
	})
	defer srv.Close()
	client, _ := newSignInTestClient(t, srv, idp)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	_, err := postDecision(context.Background(), client, mustUUID(t, stubID), "approve", "")
	if err == nil || !strings.Contains(err.Error(), "approve in the web interface") {
		t.Fatalf("err = %v, want the web interface hint", err)
	}
	if opened != 0 {
		t.Errorf("browser sign-ins = %d, want 0", opened)
	}
}

// severalIDsBackplane serves two pending requests. A request reads as
// approved after its decide. Decide accepts only "fresh-token".
func severalIDsBackplane(t *testing.T, ids []string, bearers *[]string) *httptest.Server {
	decided := map[string]bool{}
	routes := map[string]mockHandler{"GET /api/v1/auth-config": authConfigRoute(t)}
	for _, id := range ids {
		id := id
		routes["GET /api/v1/approvals/"+id] = func(w http.ResponseWriter, _ *http.Request) {
			v := newApprovalView(t, "pending", "")
			v.Id = mustUUID(t, id)
			v.OpId = "vm.resize"
			v.ConnectorId = "vmware-rest-9.0"
			v.ProposedEffect = map[string]interface{}{
				"safety_level": "caution",
				"params_echo":  map[string]interface{}{"vm": "web-01", "cpu": 4},
			}
			if decided[id] {
				v.Status = api.ApprovalRequestStatusApproved
			}
			writeJSON(t, w, http.StatusOK, v)
		}
		routes["POST /api/v1/approvals/"+id+"/decide"] = func(w http.ResponseWriter, r *http.Request) {
			bearer := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			*bearers = append(*bearers, bearer)
			if bearer != "fresh-token" {
				w.Header().Set("WWW-Authenticate", freshChallenge)
				writeJSON(t, w, http.StatusUnauthorized, map[string]string{"detail": "fresh_sign_in_required: sign in again"})
				return
			}
			decided[id] = true
			writeJSON(t, w, http.StatusOK, api.DecideResponseBody{ApprovalRequestId: mustUUID(t, id), Decision: "approved"})
		}
	}
	srv := mockBackplane(t, routes)
	t.Cleanup(srv.Close)
	return srv
}

// runSeveralIDs runs `meho approvals approve <id> <id>` with a stored
// login in a temp config dir. It returns stdout, stderr and the dir.
func runSeveralIDs(t *testing.T, srv, idp *httptest.Server, ids []string, stdin string) (string, string, string, error) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	service, user := auth.KeyForBackplane(srv.URL)
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken: "test-bearer", BackplaneURL: srv.URL, Issuer: idp.URL, ClientID: "meho-cli",
		Expiry: time.Now().Add(time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	cmd := &cobra.Command{Use: "approve"}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetIn(strings.NewReader(stdin))
	cmd.SetContext(context.Background())
	err = runApproveMany(cmd, ids, "", false, srv.URL)
	return stdout.String(), stderr.String(), dir, err
}

func TestApproveSeveralIDsWithOneSignIn(t *testing.T) {
	idp := newFakeSignInIdP(t)
	ids := []string{stubID, "22222222-2222-2222-2222-222222222222"}
	var bearers []string
	srv := severalIDsBackplane(t, ids, &bearers)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	stdout, stderr, dir, err := runSeveralIDs(t, srv, idp, ids, "yes\nyes\n")
	if err != nil {
		t.Fatalf("runApproveMany: %v\nstderr:\n%s", err, stderr)
	}
	if opened != 1 {
		t.Errorf("browser sign-ins = %d, want 1 for both requests", opened)
	}
	if strings.Join(bearers, ",") != "fresh-token,fresh-token" {
		t.Errorf("decide bearers = %v", bearers)
	}
	if n := strings.Count(stderr, "Type yes to approve request"); n != 2 {
		t.Errorf("confirmations = %d, want one per request; stderr:\n%s", n, stderr)
	}
	for _, want := range []string{"Operation:    vm.resize", "Connector:    vmware-rest-9.0", "Risk level:   caution", `"cpu": 4`} {
		if !strings.Contains(stderr, want) {
			t.Errorf("the request shown lacks %q; stderr:\n%s", want, stderr)
		}
	}
	if !strings.Contains(stdout, "Approved 2 of 2 request(s).") {
		t.Errorf("stdout:\n%s", stdout)
	}
	// The sign-in token is never written anywhere.
	if strings.Contains(stdout+stderr, "fresh-token") {
		t.Error("the sign-in token is in the output")
	}
	_ = filepath.WalkDir(dir, func(path string, d fs.DirEntry, _ error) error {
		if d != nil && !d.IsDir() {
			if b, rerr := os.ReadFile(path); rerr == nil && strings.Contains(string(b), "fresh") {
				t.Errorf("the sign-in token was written to %s", path)
			}
		}
		return nil
	})
}

func TestApproveSeveralIDsSkipsARequestWithoutYes(t *testing.T) {
	idp := newFakeSignInIdP(t)
	ids := []string{stubID, "22222222-2222-2222-2222-222222222222"}
	var bearers []string
	srv := severalIDsBackplane(t, ids, &bearers)
	opened := 0
	useBrowser(t, fakeSignInBrowser(t, &opened))

	stdout, _, _, err := runSeveralIDs(t, srv, idp, ids, "no\nyes\n")
	if err != nil {
		t.Fatalf("runApproveMany: %v", err)
	}
	if len(bearers) != 1 || opened != 1 {
		t.Errorf("decide calls = %d, sign-ins = %d; want 1 and 1", len(bearers), opened)
	}
	if !strings.Contains(stdout, stubID+" skipped: you did not type yes") ||
		!strings.Contains(stdout, "Approved 1 of 2 request(s).") {
		t.Errorf("stdout:\n%s", stdout)
	}
}
