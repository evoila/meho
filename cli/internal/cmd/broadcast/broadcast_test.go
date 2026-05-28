// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"bytes"
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// seedXDGAndToken seeds a per-test config dir + token store that
// `backplane.Resolve` and `api.NewAuthedClient` will read. Mirrors
// the helper in cli/internal/cmd/audit/audit_test.go. The token's
// access_token is a non-empty string so the bearer-injecting editor
// in `api.NewAuthedClient` doesn't short-circuit on "stored token
// has no access_token".
func seedXDGAndToken(t *testing.T, backplaneURL string) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  "eyJ.test.token",
		TokenType:    "Bearer",
		Expiry:       time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return dir
}

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers
// attached. Mirrors the audit-package helper.
func newRunCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

// TestNewRootCmdRegistersOverridesParent -- the `broadcast` parent
// mounts the `overrides` subcommand parent.
func TestNewRootCmdRegistersOverridesParent(t *testing.T) {
	root := NewRootCmd()
	found := false
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if name == "overrides" {
			found = true
		}
	}
	if !found {
		t.Errorf("subcommand %q not registered under `meho broadcast`", "overrides")
	}
}

// TestOverridesRegistersListSetRemove -- the `overrides` parent mounts
// the three CRUD verbs.
func TestOverridesRegistersListSetRemove(t *testing.T) {
	root := NewRootCmd()
	var overridesCmd *cobra.Command
	for _, sub := range root.Commands() {
		if strings.SplitN(sub.Use, " ", 2)[0] == "overrides" {
			overridesCmd = sub
			break
		}
	}
	if overridesCmd == nil {
		t.Fatal("`overrides` parent missing")
	}
	want := map[string]bool{"list": false, "set": false, "remove": false}
	for _, sub := range overridesCmd.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho broadcast overrides`", name)
		}
	}
}

// TestBackplaneNormaliseURLContract -- the shared resolver still
// strips trailing slashes; this test is here to flag any regression
// at the broadcast package's edge (the previous local normaliseURL
// helper was deleted in G0.12-T6 in favour of backplane.NormaliseURL).
func TestBackplaneNormaliseURLContract(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test:8443/")
	if err != nil {
		t.Fatalf("backplane.NormaliseURL: %v", err)
	}
	if got != "https://meho.test:8443" {
		t.Errorf("got %q; want %q", got, "https://meho.test:8443")
	}
}

// TestTrimmedBodyDropsTrailingWhitespace pins the small renderer
// helper. Backplane responses often arrive with a trailing newline;
// dropping it keeps the error envelope's `HTTP 500: foo` shape
// stable in the operator-visible output.
func TestTrimmedBodyDropsTrailingWhitespace(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"plain", "plain"},
		{"trail\n", "trail"},
		{"trail \r\n", "trail"},
		{"trail\t  ", "trail"},
		{"", "(empty body)"},
		{"   \n", "(empty body)"},
		{"   leading kept", "   leading kept"},
	}
	for _, tc := range cases {
		if got := trimmedBody([]byte(tc.in)); got != tc.want {
			t.Errorf("trimmedBody(%q) = %q; want %q", tc.in, got, tc.want)
		}
	}
}

// TestDecodeDetailRoundTripsFastAPIEnvelope -- the FastAPI
// `{"detail": "..."}` shape gets unwrapped; non-envelope bodies
// fall back to the trimmed body. The string-shape detail is the
// operator-friendly one we surface (HTTPException); a dict/list
// detail (422 validation envelope) falls back to the raw body.
func TestDecodeDetailRoundTripsFastAPIEnvelope(t *testing.T) {
	cases := []struct {
		name     string
		body     string
		fallback string
		want     string
	}{
		{"string detail", `{"detail":"broadcast_override_not_found"}`, "fallback", "broadcast_override_not_found"},
		{"empty detail", `{"detail":""}`, "fallback-on-empty", "fallback-on-empty"},
		{"dict detail", `{"detail":{"loc":["body"],"msg":"bad"}}`, "fallback-on-dict", "fallback-on-dict"},
		{"not JSON", `not json`, "fallback-on-garbage", "fallback-on-garbage"},
		{"no detail key", `{"other":"thing"}`, "fallback-no-key", "fallback-no-key"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := decodeDetail([]byte(tc.body), tc.fallback)
			if got != tc.want {
				t.Errorf("decodeDetail(%q, %q) = %q; want %q", tc.body, tc.fallback, got, tc.want)
			}
		})
	}
}
