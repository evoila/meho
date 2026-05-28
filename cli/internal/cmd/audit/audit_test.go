// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// seedXDGAndToken seeds a per-test config dir + token store that
// the typed client's `api.NewAuthedClient` will read via the default
// `auth.NewTokenStore` path. Mirrors the same helper in
// cli/internal/cmd/targets/list_test.go.
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
// attached. The runXxx helpers consume cmd.OutOrStdout /
// cmd.ErrOrStderr; tests inspect the buffers afterwards.
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

// TestNewRootCmdRegistersAllVerbs — AC1: every advertised verb has a
// cobra subcommand. The CLI manifest is the contract operators build
// muscle memory around; dropping a verb silently is the regression
// class we want to catch at unit-time. G8.2-T5 (#1013) added `replay`.
func TestNewRootCmdRegistersAllVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"query":       false,
		"recent":      false,
		"show":        false,
		"who-touched": false,
		"my-recent":   false,
		"replay":      false,
	}
	for _, sub := range root.Commands() {
		// cobra splits `Use` on the first space to render <args>
		// (so `show <audit-id>` registers as `show`). Mirror that
		// split for the contains-check.
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho audit`", name)
		}
	}
}

// TestNormaliseURLStripsTrailingSlash — the resolver mirrors the
// targets / operation helpers; the trailing slash invariant is
// load-bearing for the request paths assembled on top.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.example/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.example" {
		t.Errorf("trailing slash not stripped: got %q", got)
	}
}

// TestNormaliseURLRejectsHostlessInput — bare paths fail fast rather
// than producing a request against the local filesystem.
func TestNormaliseURLRejectsHostlessInput(t *testing.T) {
	if _, err := backplane.NormaliseURL("/just/a/path"); err == nil {
		t.Errorf("expected error for hostless URL")
	}
}

// TestNormaliseURLRejectsGarbage — a fundamentally unparseable URL
// surfaces the parse error rather than the silently-empty resolver.
func TestNormaliseURLRejectsGarbage(t *testing.T) {
	if _, err := backplane.NormaliseURL("h ttp://broken"); err == nil {
		t.Errorf("expected error for malformed URL")
	}
}

// TestNormaliseURLRejectsEmpty — empty config slot must reach the
// caller as an error rather than producing a zero-host request.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := backplane.NormaliseURL("   "); err == nil {
		t.Errorf("expected error for empty URL")
	}
}

// TestDecodeDetailStringPullsString — FastAPI's HTTPException body
// surfaces as `{"detail": "<string>"}`. The audit-API 400 path
// (DurationParseError / InvalidCursorError / UnsupportedFilterError)
// uses that shape, so the CLI's error renderer leans on this helper.
func TestDecodeDetailStringPullsString(t *testing.T) {
	body := `{"detail": "cursor is not valid base64"}`
	if got := decodeDetailString(body); got != "cursor is not valid base64" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

// TestDecodeDetailStringFallsBackOnNonJSON — operators see the raw
// body when the response isn't FastAPI-shaped (a load balancer 503,
// a stray HTML page) rather than an empty error.
func TestDecodeDetailStringFallsBackOnNonJSON(t *testing.T) {
	body := "Service Unavailable"
	if got := decodeDetailString(body); got != "Service Unavailable" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

// TestTrimmedBodyDropsTrailingWhitespace pins the small renderer
// helper. Backplane responses often arrive with a trailing newline;
// dropping it keeps the error envelope's `HTTP 500: foo` shape
// stable in the operator-visible output. Mirrors the approvals
// migration's namesake helper (G0.12-T1 #1276).
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

// TestTruncateRuneAware — multi-byte UTF-8 stays valid when the
// table renderer truncates a long target name.
func TestTruncateRuneAware(t *testing.T) {
	got := truncate("vörtex-vcenter-prod-eu-central-1", 10)
	if len(got) == 0 || !strings.Contains(got, "…") {
		t.Errorf("truncate did not emit ellipsis: %q", got)
	}
}

// TestStrDerefHandlesNil — defensive nil-deref so the table /
// summary helpers don't panic on backend-null fields.
func TestStrDerefHandlesNil(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Errorf("strDeref(nil): got %q", got)
	}
	v := "hello"
	if got := strDeref(&v); got != "hello" {
		t.Errorf("strDeref(&\"hello\"): got %q", got)
	}
}

// TestUUIDDerefHandlesNil pins the renderer's nil-UUID rendering.
// The generated client surfaces nullable UUIDs as
// `*openapi_types.UUID`; the summary's "-" rendering depends on
// the nil-safe deref.
func TestUUIDDerefHandlesNil(t *testing.T) {
	if got := uuidDeref(nil); got != "" {
		t.Errorf("uuidDeref(nil): got %q", got)
	}
}

// TestFormatTSRendersUTCRFC3339 — the generated client decodes the
// backend's ISO-8601 `ts` string into `time.Time`; the renderer
// re-serialises in UTC RFC3339 so the operator-visible column
// matches the pre-migration `Entry.TS` string field shape.
func TestFormatTSRendersUTCRFC3339(t *testing.T) {
	ts, err := time.Parse(time.RFC3339, "2026-05-13T15:42:11Z")
	if err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	got := formatTS(ts)
	if got != "2026-05-13T15:42:11Z" {
		t.Errorf("formatTS round-trip: got %q", got)
	}
}

// TestRenderHTTPStatusMaps401ToAuthExpired pins the 401 arm of the
// shared HTTP-status switch. The backend 401 surface lands here when
// a refresh failed mid-call; the operator sees a `meho login` hint.
func TestRenderHTTPStatusMaps401ToAuthExpired(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte("unauthorised"))
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runMyRecent(cmd, myRecentOptions{
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 401")
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("stderr missing login hint: %s", stderr.String())
	}
}

// TestRouteRequestErrorRoutesHTTPStatus — the dispatcher correctly
// routes an `*httpResponseError` through `renderHTTPStatus` rather
// than the transport ladder.
func TestRouteRequestErrorRoutesHTTPStatus(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	he := &httpResponseError{statusCode: http.StatusForbidden, body: []byte(`{"detail":"operator required"}`)}
	if err := routeRequestError(cmd, "https://meho.example", he, true); err == nil {
		t.Fatalf("expected error returned from routeRequestError")
	}
	if !strings.Contains(stderr.String(), "operator required") {
		t.Errorf("stderr missing 403 detail: %s", stderr.String())
	}
}

// TestRouteRequestErrorRoutesTransport — a non-HTTP error path takes
// the transport ladder (Unreachable) rather than the HTTP-status
// switch.
func TestRouteRequestErrorRoutesTransport(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := routeRequestError(cmd, "https://meho.example", context.Canceled, true); err == nil {
		t.Fatalf("expected error returned from routeRequestError")
	}
	if !strings.Contains(stderr.String(), "call ") {
		t.Errorf("stderr does not look like Unreachable: %s", stderr.String())
	}
}

// TestNewAuthedClientNoStoredTokenSurfacesAuthExpired pins the
// `renderClientError` `IsTokenNotFound` arm — invoking any verb
// against a backplane that has no stored token surfaces an
// auth_expired envelope with a `meho login` hint, before any
// network round-trip.
func TestNewAuthedClientNoStoredTokenSurfacesAuthExpired(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	if err := auth.SaveConfigAt(
		filepath.Join(dir, "meho", "config.json"),
		auth.Config{BackplaneURL: "https://meho.example"},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}

	cmd, _, stderr := newRunCmd(t)
	_, err := newAuthedClient(cmd.Context(), cmd, "https://meho.example", true)
	if err == nil {
		t.Fatalf("expected error from newAuthedClient with no stored token")
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("stderr missing login hint: %s", stderr.String())
	}
}

// TestPostQueryReturnsHTTPErrorOnNon2xx pins the `postQuery` helper
// behaviour: a non-2xx response lands as an `*httpResponseError`
// (not a transport error), so `routeRequestError` can classify it.
func TestPostQueryReturnsHTTPErrorOnNon2xx(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("forbidden"))
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)
	client, err := api.NewAuthedClient(context.Background(), srv.URL, api.AuthedClientOptions{})
	if err != nil {
		t.Fatalf("NewAuthedClient: %v", err)
	}
	_, _, err = postQuery(context.Background(), client, api.AuditQueryRequest{})
	if err == nil {
		t.Fatalf("expected error on 403")
	}
	var he *httpResponseError
	if !asHTTPResponseError(err, &he) {
		t.Errorf("expected *httpResponseError; got %T: %v", err, err)
	} else if he.statusCode != http.StatusForbidden {
		t.Errorf("statusCode: got %d; want 403", he.statusCode)
	}
}

// asHTTPResponseError is a tiny test helper that wraps errors.As so
// the cast above stays readable without dragging the std-lib import
// into the assertion site.
func asHTTPResponseError(err error, target **httpResponseError) bool {
	// Stop at the first non-wrap; this skill's errors don't nest
	// under wrap helpers, so a single type assertion is sufficient.
	if err == nil {
		return false
	}
	if h, ok := err.(*httpResponseError); ok {
		*target = h
		return true
	}
	return false
}

// TestRenderHTTPStatus404SurfacesAuditRowNotFound pins the audit-
// specific 404 arm. The cross-tenant probe always reads as 404 —
// the substrate's tenant WHERE clause yields zero rows and the
// route returns 404 rather than 403 so existence never leaks.
func TestRenderHTTPStatus404SurfacesAuditRowNotFound(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := renderHTTPStatus(cmd, "https://meho.example", http.StatusNotFound,
		[]byte(`{"detail":"audit row not found"}`), true); err == nil {
		t.Fatalf("expected error returned for 404")
	}
	if !strings.Contains(stderr.String(), "audit row not found") {
		t.Errorf("stderr missing not-found hint: %s", stderr.String())
	}
}

// TestRenderHTTPStatus400SurfacesParserDetail — 400 from the audit
// API is DurationParseError / InvalidCursorError /
// UnsupportedFilterError; the operator sees the parser's own message.
func TestRenderHTTPStatus400SurfacesParserDetail(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := renderHTTPStatus(cmd, "https://meho.example", http.StatusBadRequest,
		[]byte(`{"detail":"unrecognised duration 'foo'"}`), true); err == nil {
		t.Fatalf("expected error returned for 400")
	}
	if !strings.Contains(stderr.String(), "unrecognised duration") {
		t.Errorf("stderr missing parser detail: %s", stderr.String())
	}
}

// TestRenderHTTPStatus413DefensiveFallback — the replay verb shadows
// this arm with its own session-id-aware redirect, but a non-replay
// caller hitting 413 still gets the cap + redirect hint.
func TestRenderHTTPStatus413DefensiveFallback(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := renderHTTPStatus(cmd, "https://meho.example", http.StatusRequestEntityTooLarge,
		[]byte(`{"detail":{"detail":"session_too_large","row_count":12345}}`), true); err == nil {
		t.Fatalf("expected error returned for 413")
	}
	if !strings.Contains(stderr.String(), "12345 rows") {
		t.Errorf("stderr missing row count: %s", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho audit query") {
		t.Errorf("stderr missing redirect hint: %s", stderr.String())
	}
}

// TestRenderHTTPStatus422SurfacesValidation — FastAPI's validation
// envelope passes through with the "invalid request" prefix.
func TestRenderHTTPStatus422SurfacesValidation(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	body := []byte(`{"detail":[{"loc":["path","audit_id"],"msg":"value is not a valid uuid"}]}`)
	if err := renderHTTPStatus(cmd, "https://meho.example", http.StatusUnprocessableEntity,
		body, true); err == nil {
		t.Fatalf("expected error returned for 422")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("stderr missing validation hint: %s", stderr.String())
	}
}

// TestRenderHTTPStatusDefaultArm — anything else surfaces as
// unexpected with the raw body for the operator.
func TestRenderHTTPStatusDefaultArm(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := renderHTTPStatus(cmd, "https://meho.example", http.StatusServiceUnavailable,
		[]byte("maintenance"), true); err == nil {
		t.Fatalf("expected error returned for 503")
	}
	if !strings.Contains(stderr.String(), "HTTP 503") {
		t.Errorf("stderr missing HTTP code: %s", stderr.String())
	}
}

// mustUUID parses s as a UUID and casts to the openapi_types alias,
// failing the test on error. Used by fixtures that need a canonical
// UUID for typed `api.AuditEntry.Id` / `.TenantId` / etc. fields.
func mustUUID(t *testing.T, s string) openapi_types.UUID {
	t.Helper()
	parsed, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("mustUUID(%q): %v", s, err)
	}
	return openapi_types.UUID(parsed)
}

// mustUUIDPtr is the pointer companion to mustUUID for the
// nullable-UUID fields on `api.AuditEntry` (TenantId, TargetId,
// RequestId, ParentAuditId, AgentSessionId, BroadcastEventId).
func mustUUIDPtr(t *testing.T, s string) *openapi_types.UUID {
	t.Helper()
	u := mustUUID(t, s)
	return &u
}

// Sentinel: package-level helpers compile against the typed client's
// public surface. A regression that drops one of the imports surfaces
// as a build failure pinning the helper-shape contract.
var (
	_ = json.NewDecoder
	_ = newAuthedClient
	_ = mustUUIDPtr
	_ api.AuditEntry
)
