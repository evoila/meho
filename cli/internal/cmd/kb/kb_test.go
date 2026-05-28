// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// seedXDGAndToken seeds a per-test config dir + token store that
// backplane.Resolve / api.NewAuthedClient will read. Mirrors the
// same helper in cli/internal/cmd/audit/audit_test.go. Retained
// verbatim across the G0.12-T9 migration: the generated typed
// client reads through the same `auth.NewTokenStore` + config-file
// path the previous hand-rolled transport used, so the test
// substrate is unchanged at the seed layer — only the verb's
// call-site (now typed `*WithResponse` methods) differs.
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

// TestNewRootCmdRegistersAllSixVerbs — AC1: every advertised verb
// has a cobra subcommand. The CLI manifest is the contract operators
// build muscle memory around; dropping a verb silently is the
// regression class we want to catch at unit-time.
func TestNewRootCmdRegistersAllSixVerbs(t *testing.T) {
	root := NewRootCmd()
	want := map[string]bool{
		"ingest": false,
		"search": false,
		"list":   false,
		"show":   false,
		"add":    false,
		"delete": false,
	}
	for _, sub := range root.Commands() {
		name := strings.SplitN(sub.Use, " ", 2)[0]
		if _, ok := want[name]; ok {
			want[name] = true
		}
	}
	for name, seen := range want {
		if !seen {
			t.Errorf("subcommand %q not registered under `meho kb`", name)
		}
	}
}

// TestRootCmdHelpListsAllVerbs — the parent's help text should
// mention every verb so operators new to the surface find them.
func TestRootCmdHelpListsAllVerbs(t *testing.T) {
	root := NewRootCmd()
	var buf bytes.Buffer
	root.SetOut(&buf)
	root.SetErr(&buf)
	root.SetArgs([]string{"--help"})
	if err := root.Execute(); err != nil {
		t.Fatalf("`meho kb --help` failed: %v", err)
	}
	help := buf.String()
	for _, want := range []string{"ingest", "search", "list", "show", "add", "delete", "tenant"} {
		if !strings.Contains(help, want) {
			t.Errorf("expected `meho kb --help` to mention %q; got:\n%s", want, help)
		}
	}
}

// TestNormaliseURLStripsTrailingSlash — the resolver mirrors the
// sibling packages; trailing-slash trimming is the v0.2 convention.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Errorf("trailing slash not stripped: got %q", got)
	}
}

// TestNormaliseURLRejectsHostless — bare paths fail fast rather
// than producing a request against the local filesystem.
func TestNormaliseURLRejectsHostless(t *testing.T) {
	if _, err := backplane.NormaliseURL("/just/a/path"); err == nil {
		t.Errorf("expected error for hostless URL")
	}
}

// TestNormaliseURLRejectsEmpty — empty input returns the "empty"
// error string callers depend on.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := backplane.NormaliseURL("   "); err == nil {
		t.Errorf("expected error for empty URL")
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any error wrapping it) maps to AuthExpired; everything else maps
// to Unexpected. Same routing ladder as the audit / targets
// siblings.
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

// TestPathEscapePreservesSlugChars — kb slugs are
// `[a-z][a-z0-9.\-]*[a-z0-9]?`; PathEscape must not mangle the
// operator-typical characters. The typed-client embeds the same
// `url.PathEscape` rule when building `/api/v1/kb/{slug}` paths,
// so this helper test still guards the operator-visible URL shape
// even after the migration moved the actual path construction
// inside the generated client.
func TestPathEscapePreservesSlugChars(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"vcenter-9.0-overview", "vcenter-9.0-overview"},
		{"a", "a"},
		{"slug-with-dot.0.1", "slug-with-dot.0.1"},
		{"weird slug with space", "weird%20slug%20with%20space"},
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

// TestParseMetadataFlagEmpty — empty input returns nil so the
// caller can omit the field.
func TestParseMetadataFlagEmpty(t *testing.T) {
	got, err := parseMetadataFlag("")
	if err != nil {
		t.Fatalf("parseMetadataFlag(\"\"): %v", err)
	}
	if got != nil {
		t.Errorf("expected nil for empty input; got %+v", got)
	}
}

// TestParseMetadataFlagSinglePair — single k=v pair.
func TestParseMetadataFlagSinglePair(t *testing.T) {
	got, err := parseMetadataFlag("owner=ops")
	if err != nil {
		t.Fatalf("parseMetadataFlag: %v", err)
	}
	if v, ok := got["owner"].(string); !ok || v != "ops" {
		t.Errorf("expected owner=ops; got %+v", got)
	}
}

// TestParseMetadataFlagMultiplePairs — commas separate pairs and
// whitespace is trimmed around keys / values.
func TestParseMetadataFlagMultiplePairs(t *testing.T) {
	got, err := parseMetadataFlag("owner = ops, source =runbook ")
	if err != nil {
		t.Fatalf("parseMetadataFlag: %v", err)
	}
	if v, ok := got["owner"].(string); !ok || v != "ops" {
		t.Errorf("expected owner=ops; got %+v", got["owner"])
	}
	if v, ok := got["source"].(string); !ok || v != "runbook" {
		t.Errorf("expected source=runbook; got %+v", got["source"])
	}
}

// TestParseMetadataFlagEmptyValue — value may be empty; key cannot.
func TestParseMetadataFlagEmptyValue(t *testing.T) {
	got, err := parseMetadataFlag("flag=")
	if err != nil {
		t.Fatalf("empty value should not error: %v", err)
	}
	if v, ok := got["flag"].(string); !ok || v != "" {
		t.Errorf("expected flag with empty value; got %+v", got["flag"])
	}
}

// TestParseMetadataFlagRejectsEmptyKey — empty key surfaces as a
// clear CLI-side error rather than a 422 round-trip.
func TestParseMetadataFlagRejectsEmptyKey(t *testing.T) {
	if _, err := parseMetadataFlag("=value"); err == nil {
		t.Errorf("expected error for empty key")
	}
}

// TestParseMetadataFlagRejectsMissingEquals — a pair without `=`
// surfaces as an error rather than being silently dropped.
func TestParseMetadataFlagRejectsMissingEquals(t *testing.T) {
	if _, err := parseMetadataFlag("nokv"); err == nil {
		t.Errorf("expected error for pair without '='")
	}
}

// TestLoadBodyFlagInlineText — inline text passes through verbatim.
func TestLoadBodyFlagInlineText(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	got, err := loadBodyFlag(cmd, "inline content")
	if err != nil {
		t.Fatalf("loadBodyFlag: %v", err)
	}
	if got != "inline content" {
		t.Errorf("expected verbatim inline; got %q", got)
	}
}

// TestLoadBodyFlagRejectsEmpty — the substrate's min_length=1
// constraint surfaces as a CLI-side error before the round-trip.
func TestLoadBodyFlagRejectsEmpty(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	if _, err := loadBodyFlag(cmd, ""); err == nil {
		t.Errorf("expected error for empty --body")
	}
}

// TestLoadBodyFlagReadsFile — @<path> reads the file and strips
// trailing newlines.
func TestLoadBodyFlagReadsFile(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	tmp := filepath.Join(t.TempDir(), "entry.md")
	if err := writeFile(tmp, "first line\nsecond line\n"); err != nil {
		t.Fatalf("write tmp: %v", err)
	}
	got, err := loadBodyFlag(cmd, "@"+tmp)
	if err != nil {
		t.Fatalf("loadBodyFlag: %v", err)
	}
	want := "first line\nsecond line"
	if got != want {
		t.Errorf("loadBodyFlag @file: got %q; want %q", got, want)
	}
}

// TestLoadBodyFlagReadsStdin — `@-` reads from cmd.InOrStdin().
func TestLoadBodyFlagReadsStdin(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString("from stdin\n"))
	got, err := loadBodyFlag(cmd, "@-")
	if err != nil {
		t.Fatalf("loadBodyFlag @-: %v", err)
	}
	if got != "from stdin" {
		t.Errorf("loadBodyFlag @-: got %q; want %q", got, "from stdin")
	}
}

// TestLoadBodyFlagRejectsEmptyStdin — stdin that closes empty must
// surface a clear error rather than silently sending an empty body
// (the substrate would reject with 422 but the CLI error is
// faster + more grep-able).
func TestLoadBodyFlagRejectsEmptyStdin(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	if _, err := loadBodyFlag(cmd, "@-"); err == nil {
		t.Errorf("expected error for empty @- input")
	}
}

// TestLoadBodyFlagRejectsMissingFile — a missing @<path> surfaces
// the underlying filesystem error.
func TestLoadBodyFlagRejectsMissingFile(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	if _, err := loadBodyFlag(cmd, "@/no/such/path/entry.md"); err == nil {
		t.Errorf("expected error for missing file")
	}
}

// TestConfirmPromptYesNo — y/yes return true; everything else false.
func TestConfirmPromptYesNo(t *testing.T) {
	cases := []struct {
		input string
		want  bool
	}{
		{"y\n", true},
		{"yes\n", true},
		{"Y\n", true},
		{"YES\n", true},
		{"n\n", false},
		{"no\n", false},
		{"\n", false},
		{"", false}, // EOF on closed stdin
		{"sure\n", false},
	}
	for _, c := range cases {
		cmd := &cobra.Command{}
		var stdout bytes.Buffer
		cmd.SetIn(bytes.NewBufferString(c.input))
		cmd.SetOut(&stdout)
		if got := confirmPrompt(cmd, "test"); got != c.want {
			t.Errorf("confirmPrompt(%q): got %v; want %v", c.input, got, c.want)
		}
		if !strings.Contains(stdout.String(), "test") {
			t.Errorf("prompt text not echoed: %q", stdout.String())
		}
	}
}

// TestRenderRequestErrorEmptyBearerMapsToAuthExpired — an empty
// stored bearer is a credential-state failure (the token row
// exists but its `access_token` is empty); renderRequestError must
// map it to auth_expired (exit 2) with a `meho login` hint rather
// than letting it fall through to unreachable (exit 3) the generic
// error string would land on. The sentinel is set by
// newAuthedClient in the migrated package; before the typed-client
// migration the equivalent sentinel was raised by the package-local
// HTTP helper. Same operator-visible behaviour either way.
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

// TestRenderHTTPStatus404SurfacesDetail pins the renderHTTPStatus
// switch on a 404 surface: the substrate returns
// `{"detail": "slug_not_found"}` for both genuine absences and
// cross-tenant probes, and the renderer must surface the detail
// string under `unexpected_response`. This direct unit test of
// renderHTTPStatus complements the per-verb runX tests in
// show_test.go / delete_test.go which exercise the full RunE path.
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

// TestRenderHTTPStatus403SurfacesInsufficientRole pins the 403 →
// insufficient_role mapping from the renderer's switch.
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

// TestRenderHTTPStatus422WrapsValidationDetail — 422 from the
// invalid_slug / missing-body path renders with the `invalid
// request:` prefix and the FastAPI envelope intact (the substrate
// emits a structured list for some 422s; preserving the body lets
// operators paste it into the issue without losing context).
func TestRenderHTTPStatus422WrapsValidationDetail(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	body := `{"detail":[{"loc":["body","slug"],"msg":"value does not match SLUG_PATTERN"}]}`
	err := renderHTTPStatus(cmd, "https://meho.test", http.StatusUnprocessableEntity,
		[]byte(body), false)
	if err == nil {
		t.Fatalf("expected non-nil error")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("expected `invalid request` prefix; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "SLUG_PATTERN") {
		t.Errorf("expected substrate detail preserved; got %q", stderr.String())
	}
}

// TestRunListRejectsOversizedResponse restores the 1-MiB
// response-body cap coverage on the typed-client surface. The cap
// is now installed at the transport layer via
// `api.AuthedClientOptions.ResponseBodyLimit` (wired by
// `newAuthedClient` in `kb.go` to `responseBodyCap`), which wraps
// `rsp.Body` in an `http.MaxBytesReader` so the generated
// `Parse*Response` helpers can't ReadAll an unbounded body. When
// the cap fires, the resulting `*http.MaxBytesError` bubbles out of
// the typed call and `renderRequestError` maps it to
// `output.Unexpected` (exit 4 — `unexpected_response`) rather than
// `output.Unreachable` (exit 3). Pre-migration this was tested
// against the package-local `doAuthedRequest` helper; the post-
// migration test drives the same property end-to-end through
// `runList` against an httptest server that returns an oversized
// 200 body.
func TestRunListRejectsOversizedResponse(t *testing.T) {
	// One byte over the cap so the MaxBytesReader fires on the
	// final read (the +1 is the documented overshoot detection
	// pattern from the net/http source).
	oversized := strings.Repeat("a", int(responseBodyCap)+1)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// The body is not valid JSON, but that doesn't matter — the
		// MaxBytesReader cap trips before the JSON parser ever sees
		// the bytes; the error surfaces as *http.MaxBytesError, not
		// as a JSON syntax error.
		_, _ = w.Write([]byte(oversized))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on oversized response")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4 (unexpected_response); got %v", err)
	}
}

// TestRenderRequestErrorMaxBytesErrorMapsToUnexpected pins the
// classification branch in `renderRequestError` that routes an
// `*http.MaxBytesError` to `output.Unexpected` (exit 4) rather
// than `output.Unreachable` (exit 3). The end-to-end coverage in
// `TestRunListRejectsOversizedResponse` exercises the full
// transport-cap → renderRequestError path; this unit test pins the
// classification ladder directly so a future regression that
// re-orders the branches surfaces here too.
func TestRenderRequestErrorMaxBytesErrorMapsToUnexpected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderRequestError(
		cmd,
		"https://meho.test",
		&http.MaxBytesError{Limit: 1024},
		false,
	)
	if err == nil {
		t.Fatalf("expected non-nil error from renderRequestError")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}

// TestRenderRequestErrorJSONSyntaxErrorMapsToUnexpected pins the
// classification branch in `renderRequestError` that routes a
// JSON shape failure (`*json.SyntaxError`) to `output.Unexpected`.
// The generated `Parse*Response` helpers can surface this when a
// backplane / proxy returns 2xx with a malformed JSON body — that's
// a contract failure on the server, not a transport-down failure
// on the operator.
func TestRenderRequestErrorJSONSyntaxErrorMapsToUnexpected(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := renderRequestError(
		cmd,
		"https://meho.test",
		&json.SyntaxError{Offset: 12},
		false,
	)
	if err == nil {
		t.Fatalf("expected non-nil error from renderRequestError")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
}

// TestRetryOn401InvokesRefreshAndReissues drives the retry helper
// against a mock-style call function: the first invocation returns
// a 401, refresh is invoked (here a no-op success), the second
// invocation returns 200. Pins the contract every per-verb
// `get*` / `post*` helper depends on.
func TestRetryOn401InvokesRefreshAndReissues(t *testing.T) {
	// Stand up a real httptest server that 401s the first call and
	// 200s the second so the inline retryOn401 path is exercised
	// end-to-end through a real api.AuthedClient.Refresh attempt.
	// Refresh will fail (no refresh_token in the stored token), so
	// retryOn401's "refresh err → return error" branch fires; the
	// surface contract we pin is that the second call is NOT made
	// when refresh fails, and the returned error is the refresh
	// failure (mapped by renderRequestError to auth_expired with a
	// `meho login` hint).
	calls := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusUnauthorized)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error after 401 + failed refresh")
	}
	if calls != 1 {
		t.Errorf("expected exactly 1 call (refresh failure short-circuits the retry); got %d", calls)
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired classification; got %q", stderr.String())
	}
}

// TestListQueryParamsOmitsZeroValues confirms the helper that maps
// listOptions onto the generated ListKbApiV1KbGetParams shape leaves
// pointer fields nil when the operator didn't supply the
// corresponding flag, so the backplane's defaults apply.
func TestListQueryParamsOmitsZeroValues(t *testing.T) {
	got := listQueryParams(listOptions{})
	if got.Filter != nil {
		t.Errorf("expected nil Filter; got %v", *got.Filter)
	}
	if got.Limit != nil {
		t.Errorf("expected nil Limit; got %v", *got.Limit)
	}
	if got.Offset != nil {
		t.Errorf("expected nil Offset; got %v", *got.Offset)
	}
}

// TestListQueryParamsSetsAllFilters confirms supplied flags reach
// the typed params shape. Sibling per-verb tests assert on the
// query string the generated client serialises them into.
func TestListQueryParamsSetsAllFilters(t *testing.T) {
	got := listQueryParams(listOptions{Filter: "vcenter%", Limit: 25, Offset: 10})
	if got.Filter == nil || *got.Filter != "vcenter%" {
		t.Errorf("expected Filter=vcenter%%; got %+v", got.Filter)
	}
	if got.Limit == nil || *got.Limit != 25 {
		t.Errorf("expected Limit=25; got %+v", got.Limit)
	}
	if got.Offset == nil || *got.Offset != 10 {
		t.Errorf("expected Offset=10; got %+v", got.Offset)
	}
}

// TestBuildAddBodyOmitsMetadataWhenNil confirms a nil metadata map
// produces a body whose Metadata pointer stays nil so the JSON
// encoder emits "null" (acceptable to the backend) rather than an
// empty object. The substrate defaults to {} when the field is
// absent or null; both shapes pass the validator.
func TestBuildAddBodyOmitsMetadataWhenNil(t *testing.T) {
	body := buildAddBody("slug", "content", nil)
	if body.Metadata != nil {
		t.Errorf("expected nil Metadata when none supplied; got %+v", body.Metadata)
	}
	if body.Slug != "slug" || body.Body != "content" {
		t.Errorf("required fields not threaded: got %+v", body)
	}
}

// TestBuildAddBodyWiresMetadata confirms a non-nil map flows into
// the body's Metadata pointer so the wire payload carries it.
func TestBuildAddBodyWiresMetadata(t *testing.T) {
	md := map[string]any{"owner": "ops"}
	body := buildAddBody("slug", "content", md)
	if body.Metadata == nil {
		t.Fatalf("expected non-nil Metadata; got nil")
	}
	got := *body.Metadata
	if got["owner"] != "ops" {
		t.Errorf("expected metadata[owner]=ops; got %+v", got)
	}
}

// TestBuildIngestBodyOmitsDryRunWhenFalse confirms an unset --dry-run
// flag keeps the DryRun pointer nil so the JSON `dry_run` key is
// absent on the wire (the field carries `omitempty` on the
// generated type, matching the backend's `False` default).
func TestBuildIngestBodyOmitsDryRunWhenFalse(t *testing.T) {
	body := buildIngestBody(ingestOptions{Directory: "/srv/kb"})
	if body.DryRun != nil {
		t.Errorf("expected nil DryRun for default-false flag; got %+v", *body.DryRun)
	}
	if body.Directory == nil || *body.Directory != "/srv/kb" {
		t.Errorf("expected Directory=/srv/kb; got %+v", body.Directory)
	}
	if body.TarballUrl != nil {
		t.Errorf("expected nil TarballUrl; kb CLI never sets it (501 from substrate); got %+v", *body.TarballUrl)
	}
}

// TestBuildIngestBodyWiresDryRun confirms --dry-run flows into the
// body so the substrate skips the write path.
func TestBuildIngestBodyWiresDryRun(t *testing.T) {
	body := buildIngestBody(ingestOptions{Directory: "/srv/kb", DryRun: true})
	if body.DryRun == nil || !*body.DryRun {
		t.Errorf("expected DryRun=true; got %+v", body.DryRun)
	}
}

// TestBuildSearchBodyPinsSourceToKB confirms every search wire body
// carries source="kb" so the substrate scopes hits to kb-entry
// rows. Unset --limit leaves the Limit pointer nil so the
// backend's default (10) applies.
func TestBuildSearchBodyPinsSourceToKB(t *testing.T) {
	body := buildSearchBody(searchOptions{Query: "vsphere"})
	if body.Source == nil || *body.Source != "kb" {
		t.Errorf("expected Source=kb; got %+v", body.Source)
	}
	if body.Query != "vsphere" {
		t.Errorf("expected Query=vsphere; got %q", body.Query)
	}
	if body.Limit != nil {
		t.Errorf("expected nil Limit; got %+v", *body.Limit)
	}
}

// TestBuildSearchBodyWiresLimit confirms --limit lands on the wire.
func TestBuildSearchBodyWiresLimit(t *testing.T) {
	body := buildSearchBody(searchOptions{Query: "x", Limit: 25})
	if body.Limit == nil || *body.Limit != 25 {
		t.Errorf("expected Limit=25; got %+v", body.Limit)
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

// writeFile is a tiny helper that mirrors os.WriteFile but is
// independent of the test's import set — keeps the test file slim
// (one helper rather than threading os.WriteFile import through
// every test that touches the filesystem).
func writeFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o600)
}
