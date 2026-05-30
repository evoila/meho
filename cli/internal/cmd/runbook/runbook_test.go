// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// seedXDGAndToken seeds a per-test config dir + token store that
// backplane.Resolve / api.NewAuthedClient will read. Mirrors the kb /
// audit / memory tree helpers verbatim — the runbook tree consumes
// the same auth seam, so a single helper shape works.
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
// attached. The runXxx helpers consume cmd.OutOrStdout / cmd.ErrOrStderr;
// tests inspect the buffers afterwards.
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

// TestNewRootCmdRegistersAllElevenVerbs — AC1 + issue test #20:
// every advertised verb has a cobra subcommand. The CLI manifest is
// the contract operators build muscle memory around; dropping a
// verb silently is the regression class we want to catch at unit-
// time. T1 (#1318) registered six template verbs; T2 (#1319)
// extends with five run verbs, eleven total.
func TestNewRootCmdRegistersAllElevenVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		// T1 template verbs (#1318)
		"list-templates":     false,
		"show-template":      false,
		"draft-template":     false,
		"edit-template":      false,
		"publish-template":   false,
		"deprecate-template": false,
		// T2 run verbs (#1319)
		"start":    false,
		"next":     false,
		"abort":    false,
		"reassign": false,
		"runs":     false,
	}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho runbook`", name)
		}
	}
	// Parity: confirm the test enumerates all currently-registered
	// subcommands. If a future verb gets added to NewRootCmd
	// without an entry in `want`, this assertion forces the test
	// author to update both surfaces in lock-step.
	got := make(map[string]bool, len(root.Commands()))
	for _, sub := range root.Commands() {
		got[strings.SplitN(sub.Use, " ", 2)[0]] = true
	}
	if len(got) != len(want) {
		t.Errorf("registered verbs count: got %d, want %d (got=%v)",
			len(got), len(want), got)
	}
}

// TestRootCmdHelpListsAllVerbs — the parent's help text should
// mention every verb so operators new to the surface find them.
// Issue test #20.
func TestRootCmdHelpListsAllVerbs(t *testing.T) {
	root := NewRootCmd()
	var buf bytes.Buffer
	root.SetOut(&buf)
	root.SetErr(&buf)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("`meho runbook --help` failed: %v", err)
	}
	help := buf.String()
	for _, want := range []string{
		// Template verbs (T1)
		"list-templates", "show-template", "draft-template",
		"edit-template", "publish-template", "deprecate-template",
		"tenant_admin",
		// Run verbs (T2)
		"start", "next", "abort", "reassign", "runs",
		// Opacity language is a load-bearing piece of the
		// parent's help text -- regressing it would mean the
		// surface no longer documents the contract operators
		// rely on.
		"OPACITY",
	} {
		if !strings.Contains(help, want) {
			t.Errorf("expected `meho runbook --help` to mention %q; got:\n%s", want, help)
		}
	}
}

// TestDecodeDetailStringFromFastAPI — FastAPI's HTTPException body
// is {"detail": "<string>"}; decodeDetailString must extract it.
func TestDecodeDetailStringFromFastAPI(t *testing.T) {
	body := `{"detail": "slug_not_found"}`
	if got := decodeDetailString(body); got != "slug_not_found" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

// TestDecodeDetailStringFallback — non-FastAPI body returns the raw
// trimmed body rather than swallowing it.
func TestDecodeDetailStringFallback(t *testing.T) {
	body := "  plain text error\n"
	if got := decodeDetailString(body); got != "plain text error" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

// TestPathEscapePreservesSlugChars — runbook slugs are the kb slug
// pattern verbatim; PathEscape must not mangle operator-typical
// characters (dots, hyphens, digits). The typed-client embeds the
// same `url.PathEscape` rule when building
// `/api/v1/runbooks/templates/{slug}` paths.
func TestPathEscapePreservesSlugChars(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"vcenter-cert-rotation", "vcenter-cert-rotation"},
		{"vcenter-9.0-overview", "vcenter-9.0-overview"},
		{"a", "a"},
		{"slug-with-dot.0.1", "slug-with-dot.0.1"},
	}
	for _, c := range cases {
		if got := pathEscape(c.in); got != c.want {
			t.Errorf("pathEscape(%q): got %q; want %q", c.in, got, c.want)
		}
	}
}

// TestTruncateRuneAware — multi-byte UTF-8 stays valid when the
// table renderer truncates a long slug / preview.
func TestTruncateRuneAware(t *testing.T) {
	if got := truncate("café", 3); got != "ca…" {
		t.Fatalf("truncate multi-byte: got %q; want %q", got, "ca…")
	}
	if got := truncate("hello", 10); got != "hello" {
		t.Fatalf("truncate within budget: got %q; want %q", got, "hello")
	}
	if got := truncate("anything", 0); got != "" {
		t.Fatalf("truncate maxLen=0 should be empty; got %q", got)
	}
}

// TestRenderRequestErrorEmptyBearerMapsToAuthExpired — an empty
// stored bearer is a credential-state failure (the token row exists
// but its access_token is empty); renderRequestError must map it to
// auth_expired (exit 2) with a `meho login` hint.
func TestRenderRequestErrorEmptyBearerMapsToAuthExpired(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderRequestError(cmd, "https://meho.test", errMissingAccessToken, false)
	if err == nil {
		t.Fatalf("expected non-nil error from renderRequestError")
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 2 {
		t.Errorf("expected ExitCode 2 (auth_expired); got %v", err)
	}
}

// TestRenderHTTPStatus404SurfacesDetail pins the 404 → unexpected
// mapping for `slug_not_found`.
func TestRenderHTTPStatus404SurfacesDetail(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusNotFound,
		[]byte(`{"detail":"slug_not_found"}`), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected detail in stderr; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
}

// TestRenderHTTPStatus403SurfacesInsufficientRole — backend's
// tenant_admin-required detail survives the round-trip into stderr.
// The issue body's AC ties role denial to "This verb requires
// TENANT_ADMIN role" — we surface whatever the backend rendered to
// keep terminology aligned with the MCP tool description path.
func TestRenderHTTPStatus403SurfacesInsufficientRole(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusForbidden,
		[]byte(`{"detail":"Insufficient role: tenant_admin required"}`), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected backend detail in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5 (insufficient_role); got %v", err)
	}
}

// TestRenderHTTPStatus409SurfacesDetail — draft_already_exists from
// POST against an existing draft. The 409 case is unique to the
// runbook surface (vs the kb tree's renderer) so it gets a direct
// test.
func TestRenderHTTPStatus409SurfacesDetail(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusConflict,
		[]byte(`{"detail":"draft_already_exists"}`), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "draft_already_exists") {
		t.Errorf("expected detail in stderr; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
}

// TestRenderHTTPStatus422WrapsValidationDetail — 422 from the
// disallowed-substitution path renders with the `invalid request:`
// prefix and the FastAPI envelope intact (the substrate emits a
// structured list for some 422s; preserving the body lets operators
// paste it into the issue without losing context).
func TestRenderHTTPStatus422WrapsValidationDetail(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	body := `{"detail":[{"loc":["body","body","steps",0,"body"],"msg":"disallowed substitution pattern: ${evil}"}]}`
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusUnprocessableEntity,
		[]byte(body), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("expected `invalid request` prefix; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "disallowed substitution") {
		t.Errorf("expected substrate detail preserved; got %q", stderr.String())
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any error wrapping it) maps to AuthExpired; everything else maps
// to Unexpected. Same routing ladder as the kb sibling.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrappedNotFound := &backplane.NotConfiguredError{Inner: auth.ErrConfigNotFound}
	se := backplane.ClassifyError(wrappedNotFound)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("ErrConfigNotFound wrapper should classify as auth_expired; got %+v", se)
	}
	parseFailure := errors.New("invalid URL")
	se = backplane.ClassifyError(parseFailure)
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected; got %+v", se)
	}
}

// readJSONBodyOf decodes a request body for handler assertions. Kept
// in the shared helper file so per-verb tests don't each need a
// local decoder helper.
func readJSONBodyOf(t *testing.T, raw []byte, into any) {
	t.Helper()
	if err := json.Unmarshal(raw, into); err != nil {
		t.Fatalf("decode body: %v\n%s", err, raw)
	}
}
