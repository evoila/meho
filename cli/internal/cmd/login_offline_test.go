// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"

	"golang.org/x/oauth2"

	"github.com/evoila/meho/cli/internal/auth"
)

// scopeFakeIdP is a device-flow login server that records the scope of
// each device request. With refuseOffline it answers a request that
// asks for offline_access the way Keycloak does when the client does
// not offer the scope: HTTP 400, invalid_scope.
type scopeFakeIdP struct {
	srv           *httptest.Server
	refuseOffline bool
	mu            sync.Mutex
	scopes        [][]string
}

func newScopeFakeIdP(t *testing.T, refuseOffline bool) *scopeFakeIdP {
	t.Helper()
	f := &scopeFakeIdP{refuseOffline: refuseOffline}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/device":
			scope := strings.Fields(r.PostForm.Get("scope"))
			f.mu.Lock()
			f.scopes = append(f.scopes, scope)
			f.mu.Unlock()
			if f.refuseOffline && slices.Contains(scope, "offline_access") {
				w.WriteHeader(http.StatusBadRequest)
				_ = json.NewEncoder(w).Encode(map[string]string{
					"error":             "invalid_scope",
					"error_description": "Invalid scopes: " + r.PostForm.Get("scope"),
				})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"device_code":      "dc",
				"user_code":        "ABCD-EFGH",
				"verification_uri": "https://kc.example/device",
				"expires_in":       60,
				"interval":         1,
			})
		case "/token":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token":  "at",
				"token_type":    "Bearer",
				"refresh_token": "rt",
				"expires_in":    300,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *scopeFakeIdP) login(t *testing.T, scopes []string, offline bool) (*auth.LoginResult, string, int) {
	t.Helper()
	doc := &auth.DiscoveryDocument{
		Issuer:                      f.srv.URL,
		DeviceAuthorizationEndpoint: f.srv.URL + "/device",
		TokenEndpoint:               f.srv.URL + "/token",
	}
	prompts := 0
	var errOut bytes.Buffer
	result, err := deviceLogin(context.Background(), doc, "meho-cli", auth.DeviceFlowOptions{
		HTTPClient: f.srv.Client(),
		Scopes:     scopes,
		Prompter: func(context.Context, *oauth2.DeviceAuthResponse) error {
			prompts++
			return nil
		},
	}, offline, &errOut)
	if err != nil {
		t.Fatalf("deviceLogin: %v", err)
	}
	return result, errOut.String(), prompts
}

func TestLoginOfflineFlagIsOnByDefault(t *testing.T) {
	f := newLoginCmd().Flags().Lookup("offline")
	if f == nil || f.DefValue != "true" {
		t.Fatalf("--offline default = %v, want true", f)
	}
}

func TestDeviceLoginAsksForOfflineAccessByDefault(t *testing.T) {
	idp := newScopeFakeIdP(t, false)
	_, errOut, prompts := idp.login(t, nil, true)

	want := [][]string{{"openid", "offline_access"}}
	if !slices.EqualFunc(idp.scopes, want, slices.Equal[[]string]) {
		t.Errorf("scopes = %v, want %v", idp.scopes, want)
	}
	if errOut != "" || prompts != 1 {
		t.Errorf("errOut = %q, prompts = %d; want no note and one prompt", errOut, prompts)
	}
}

func TestDeviceLoginWithOfflineFalseAsksOnlyForOpenID(t *testing.T) {
	idp := newScopeFakeIdP(t, false)
	idp.login(t, nil, false)

	if len(idp.scopes) != 1 || slices.Contains(idp.scopes[0], "offline_access") {
		t.Errorf("scopes = %v, want one request without offline_access", idp.scopes)
	}
}

// An older realm does not offer offline_access to meho-cli. The login
// goes on without it, with one line that says so, and one prompt.
func TestDeviceLoginFallsBackWhenTheRealmRefusesOfflineAccess(t *testing.T) {
	idp := newScopeFakeIdP(t, true)
	result, errOut, prompts := idp.login(t, nil, true)

	want := [][]string{{"openid", "offline_access"}, {"openid"}}
	if !slices.EqualFunc(idp.scopes, want, slices.Equal[[]string]) {
		t.Errorf("scopes = %v, want %v", idp.scopes, want)
	}
	if result.Token.AccessToken != "at" {
		t.Errorf("access token = %q, want at", result.Token.AccessToken)
	}
	if errOut != offlineFallbackNote+"\n" {
		t.Errorf("errOut = %q, want the one fallback line", errOut)
	}
	for _, want := range []string{"30 minutes", "meho admin keycloak bootstrap-clients"} {
		if !strings.Contains(errOut, want) {
			t.Errorf("fallback line misses %q: %q", want, errOut)
		}
	}
	if prompts != 1 {
		t.Errorf("prompts = %d, want 1", prompts)
	}
}

// A person who names offline_access in --scope asked for it on purpose:
// no silent retry without it.
func TestDeviceLoginDoesNotDropAScopeThePersonAskedFor(t *testing.T) {
	idp := newScopeFakeIdP(t, true)
	doc := &auth.DiscoveryDocument{
		Issuer:                      idp.srv.URL,
		DeviceAuthorizationEndpoint: idp.srv.URL + "/device",
		TokenEndpoint:               idp.srv.URL + "/token",
	}
	var errOut bytes.Buffer
	_, err := deviceLogin(context.Background(), doc, "meho-cli", auth.DeviceFlowOptions{
		HTTPClient: idp.srv.Client(),
		Scopes:     []string{"openid", "offline_access"},
		Prompter:   func(context.Context, *oauth2.DeviceAuthResponse) error { return nil },
	}, true, &errOut)
	if err == nil || !auth.IsInvalidScope(err) {
		t.Fatalf("err = %v, want invalid_scope", err)
	}
	if len(idp.scopes) != 1 || errOut.Len() != 0 {
		t.Errorf("scopes = %v, errOut = %q; want one request and no note", idp.scopes, errOut.String())
	}
}
