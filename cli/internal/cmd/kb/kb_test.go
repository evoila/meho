// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"context"
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
)

// seedXDGAndToken seeds a per-test config dir + token store that
// resolveBackplane / doAuthedRequest will read. Mirrors the same
// helper in cli/internal/cmd/audit/audit_test.go.
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
	got, err := normaliseURL("https://meho.test/")
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
	if _, err := normaliseURL("/just/a/path"); err == nil {
		t.Errorf("expected error for hostless URL")
	}
}

// TestNormaliseURLRejectsEmpty — empty input returns the "empty"
// error string callers depend on.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := normaliseURL("   "); err == nil {
		t.Errorf("expected error for empty URL")
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any error wrapping it) maps to AuthExpired; everything else maps
// to Unexpected. Same routing ladder as the audit / targets
// siblings.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrappedNotFound := &errNoBackplaneConfigured{inner: auth.ErrConfigNotFound}
	se := classifyBackplaneError(wrappedNotFound)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("ErrConfigNotFound wrapper should classify as auth_expired; got %+v", se)
	}
	parseFailure := errors.New("invalid URL")
	se = classifyBackplaneError(parseFailure)
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
// operator-typical characters.
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

// TestDoAuthedRequestRejectsOversizedResponse — the response-body
// cap is paired with a +1-byte read so a response that fills the
// cap surfaces as a clear error rather than feeding a truncated
// body into the JSON decoder.
func TestDoAuthedRequestRejectsOversizedResponse(t *testing.T) {
	oversized := strings.Repeat("a", int(responseBodyCap)+1)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/test", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(oversized))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err := doAuthedRequest(ctx, srv.URL, "GET", "/api/v1/kb/test", nil)
	if err == nil {
		t.Fatalf("expected error on oversized response")
	}
	if !strings.Contains(err.Error(), "exceeds") {
		t.Errorf("error message does not mention size cap: %v", err)
	}
}

// TestRenderRequestErrorEmptyBearerMapsToAuthExpired — an empty
// stored bearer is a credential-state failure (the token row
// exists but its `access_token` is empty); renderRequestError must
// map it to auth_expired (exit 2) with a `meho login` hint rather
// than letting it fall through to unreachable (exit 3) the generic
// error string would land on.
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

// TestDoAuthedRequestHandles204NoContent — the kb.delete route
// returns 204 with an empty body whether or not the row existed;
// doAuthedRequest must treat that as success (returning nil bytes)
// rather than as a non-2xx httpError.
func TestDoAuthedRequestHandles204NoContent(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb/some-slug", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("expected DELETE; got %s", r.Method)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	raw, err := doAuthedRequest(ctx, srv.URL, "DELETE", "/api/v1/kb/some-slug", nil)
	if err != nil {
		t.Fatalf("204 should not produce an error: %v", err)
	}
	if raw != nil {
		t.Errorf("expected nil body on 204; got %q", string(raw))
	}
}

// writeFile is a tiny helper that mirrors os.WriteFile but is
// independent of the test's import set — keeps the test file slim
// (one helper rather than threading os.WriteFile import through
// every test that touches the filesystem).
func writeFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o600)
}
