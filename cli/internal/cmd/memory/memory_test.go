// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/auth"
)

// seedXDGAndToken seeds a per-test config dir + token store the way
// the sibling test files do. Mirrors the helper in
// cli/internal/cmd/kb/kb_test.go.
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

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers.
// The runXxx helpers consume cmd.OutOrStdout / cmd.ErrOrStderr;
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

// fixedNow returns a deterministic clock for parseTTLFlag tests.
func fixedNow() time.Time {
	return time.Date(2026, 5, 19, 12, 0, 0, 0, time.UTC)
}

// ---------------------------------------------------------------
// Helpers — pure functions exercised first because the verbs build
// on them.
// ---------------------------------------------------------------

func TestParseScopeAcceptsAllFive(t *testing.T) {
	cases := []struct {
		in   string
		want Scope
	}{
		{"user", ScopeUser},
		{"user-tenant", ScopeUserTenant},
		{"user-target", ScopeUserTarget},
		{"tenant", ScopeTenant},
		{"target", ScopeTarget},
	}
	for _, c := range cases {
		got, err := parseScope(c.in)
		if err != nil {
			t.Errorf("parseScope(%q): %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("parseScope(%q) = %q; want %q", c.in, got, c.want)
		}
	}
}

func TestParseScopeRejectsTypo(t *testing.T) {
	if _, err := parseScope("Tenant"); err == nil {
		t.Errorf("expected error for case-mismatched scope")
	}
	if _, err := parseScope("user_tenant"); err == nil {
		t.Errorf("expected error for underscore-separator scope")
	}
	if _, err := parseScope(""); err == nil {
		t.Errorf("expected error for empty scope")
	}
}

func TestParseScopeSlugArgHappyPath(t *testing.T) {
	scope, slug, err := parseScopeSlugArg("user-tenant/wine-preference")
	if err != nil {
		t.Fatalf("parseScopeSlugArg: %v", err)
	}
	if scope != ScopeUserTenant {
		t.Errorf("expected user-tenant; got %q", scope)
	}
	if slug != "wine-preference" {
		t.Errorf("expected slug; got %q", slug)
	}
}

func TestParseScopeSlugArgRejectsNoSeparator(t *testing.T) {
	if _, _, err := parseScopeSlugArg("user-tenant-wine-preference"); err == nil {
		t.Errorf("expected error when '/' separator absent")
	}
}

func TestParseScopeSlugArgRejectsEmpty(t *testing.T) {
	if _, _, err := parseScopeSlugArg(""); err == nil {
		t.Errorf("expected error for empty arg")
	}
	if _, _, err := parseScopeSlugArg("   "); err == nil {
		t.Errorf("expected error for whitespace-only arg")
	}
}

func TestParseScopeSlugArgRejectsEmptySide(t *testing.T) {
	if _, _, err := parseScopeSlugArg("/wine-preference"); err == nil {
		t.Errorf("expected error for empty scope")
	}
	if _, _, err := parseScopeSlugArg("user/"); err == nil {
		t.Errorf("expected error for empty slug")
	}
}

func TestParseScopeSlugArgRejectsExtraSlash(t *testing.T) {
	// A slug like `foo/bar` would violate the substrate's
	// SLUG_PATTERN; reject before the round-trip so the operator
	// fix is obvious.
	if _, _, err := parseScopeSlugArg("user/foo/bar"); err == nil {
		t.Errorf("expected error for second '/' inside slug")
	}
}

func TestParseTTLEmptyReturnsEmpty(t *testing.T) {
	got, err := parseTTLFlag("", fixedNow)
	if err != nil {
		t.Fatalf("parseTTL empty: %v", err)
	}
	if got != "" {
		t.Errorf("expected empty string for empty input; got %q", got)
	}
}

func TestParseTTLDayShorthand(t *testing.T) {
	got, err := parseTTLFlag("7d", fixedNow)
	if err != nil {
		t.Fatalf("parseTTL 7d: %v", err)
	}
	// 2026-05-19T12:00:00Z + 7 * 24h = 2026-05-26T12:00:00Z.
	want := "2026-05-26T12:00:00Z"
	if got != want {
		t.Errorf("parseTTL 7d: got %q; want %q", got, want)
	}
}

func TestParseTTLHourShorthand(t *testing.T) {
	got, err := parseTTLFlag("36h", fixedNow)
	if err != nil {
		t.Fatalf("parseTTL 36h: %v", err)
	}
	want := "2026-05-21T00:00:00Z"
	if got != want {
		t.Errorf("parseTTL 36h: got %q; want %q", got, want)
	}
}

func TestParseTTLMinuteShorthand(t *testing.T) {
	got, err := parseTTLFlag("30m", fixedNow)
	if err != nil {
		t.Fatalf("parseTTL 30m: %v", err)
	}
	want := "2026-05-19T12:30:00Z"
	if got != want {
		t.Errorf("parseTTL 30m: got %q; want %q", got, want)
	}
}

func TestParseTTLRejectsNegative(t *testing.T) {
	if _, err := parseTTLFlag("-1d", fixedNow); err == nil {
		t.Errorf("expected error for negative TTL")
	}
	// Bare `0` has no unit; ParseDuration would reject; either way an
	// error is fine.
	if _, err := parseTTLFlag("0d", fixedNow); err == nil {
		t.Errorf("expected error for zero-day TTL")
	}
}

func TestParseTTLRejectsGarbage(t *testing.T) {
	if _, err := parseTTLFlag("seven days", fixedNow); err == nil {
		t.Errorf("expected error for prose duration")
	}
}

func TestParseTagsFlagDropsEmpty(t *testing.T) {
	got := parseTagsFlag([]string{"keep", "", "  ", "also-keep"})
	if len(got) != 2 || got[0] != "keep" || got[1] != "also-keep" {
		t.Errorf("expected [keep also-keep]; got %+v", got)
	}
}

func TestParseTagsFlagNilForEmpty(t *testing.T) {
	if got := parseTagsFlag([]string{"", "  "}); got != nil {
		t.Errorf("expected nil for all-empty tags; got %+v", got)
	}
	if got := parseTagsFlag(nil); got != nil {
		t.Errorf("expected nil for nil input; got %+v", got)
	}
}

func TestRequireTargetForScopePasses(t *testing.T) {
	// Non-target scopes never need --target.
	for _, s := range []Scope{ScopeUser, ScopeUserTenant, ScopeTenant} {
		if err := requireTargetForScope(s, ""); err != nil {
			t.Errorf("scope=%s should not require target; got %v", s, err)
		}
	}
}

func TestRequireTargetForScopeBlocks(t *testing.T) {
	for _, s := range []Scope{ScopeUserTarget, ScopeTarget} {
		if err := requireTargetForScope(s, ""); err == nil {
			t.Errorf("scope=%s should require --target", s)
		}
		if err := requireTargetForScope(s, "rdc-vault"); err != nil {
			t.Errorf("scope=%s with target should pass; got %v", s, err)
		}
	}
}

func TestNormaliseURLHappy(t *testing.T) {
	got, err := normaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Errorf("trailing slash not stripped: %q", got)
	}
}

func TestNormaliseURLRejectsHostless(t *testing.T) {
	if _, err := normaliseURL("/just/a/path"); err == nil {
		t.Errorf("expected error for hostless URL")
	}
}

func TestNormaliseURLRejectsEmpty(t *testing.T) {
	if _, err := normaliseURL("   "); err == nil {
		t.Errorf("expected error for empty URL")
	}
}

func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrapped := &errNoBackplaneConfigured{inner: auth.ErrConfigNotFound}
	se := classifyBackplaneError(wrapped)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("wrapped ErrConfigNotFound should classify as auth_expired; got %+v", se)
	}
	other := errors.New("invalid URL")
	se = classifyBackplaneError(other)
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("other errors should classify as unexpected; got %+v", se)
	}
}

func TestDecodeDetailStringFromFastAPI(t *testing.T) {
	body := `{"detail": "memory_not_found"}`
	if got := decodeDetailString(body); got != "memory_not_found" {
		t.Errorf("decodeDetailString: got %q", got)
	}
}

func TestDecodeDetailStringFallback(t *testing.T) {
	if got := decodeDetailString("  plain text\n"); got != "plain text" {
		t.Errorf("decodeDetailString fallback: got %q", got)
	}
}

func TestTruncateRuneAware(t *testing.T) {
	if got := truncate("café", 3); got != "ca…" {
		t.Errorf("truncate multi-byte: got %q", got)
	}
	if got := truncate("short", 10); got != "short" {
		t.Errorf("truncate within budget: got %q", got)
	}
}

func TestPathEscapePreservesSlugChars(t *testing.T) {
	if got := pathEscape("k8s.rollout-note"); got != "k8s.rollout-note" {
		t.Errorf("PathEscape stripped legal slug chars; got %q", got)
	}
}

func TestLoadBodyInline(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	got, err := loadBody(cmd, "inline content")
	if err != nil {
		t.Fatalf("loadBody inline: %v", err)
	}
	if got != "inline content" {
		t.Errorf("expected verbatim inline; got %q", got)
	}
}

func TestLoadBodyStdin(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString("piped body\n"))
	got, err := loadBody(cmd, "-")
	if err != nil {
		t.Fatalf("loadBody stdin: %v", err)
	}
	if got != "piped body" {
		t.Errorf("expected stripped trailing newline; got %q", got)
	}
}

func TestLoadBodyRejectsEmptyStdin(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	if _, err := loadBody(cmd, "-"); err == nil {
		t.Errorf("expected error for empty stdin")
	}
}

func TestLoadBodyRejectsEmptyInline(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewBufferString(""))
	if _, err := loadBody(cmd, ""); err == nil {
		t.Errorf("expected error for empty inline body")
	}
	if _, err := loadBody(cmd, "   "); err == nil {
		t.Errorf("expected error for whitespace-only body")
	}
}

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
		if got := confirmPrompt(cmd, &stdout, "test"); got != c.want {
			t.Errorf("confirmPrompt(%q): got %v; want %v", c.input, got, c.want)
		}
		if !strings.Contains(stdout.String(), "test") {
			t.Errorf("prompt text not echoed: %q", stdout.String())
		}
	}
}

func TestBuildRecallPathBasic(t *testing.T) {
	got := buildRecallPath(ScopeUserTenant, "wine-preference", "")
	want := "/api/v1/memory/user-tenant/wine-preference"
	if got != want {
		t.Errorf("buildRecallPath: got %q; want %q", got, want)
	}
}

func TestBuildRecallPathWithTarget(t *testing.T) {
	got := buildRecallPath(ScopeTarget, "rollout", "rke2-meho")
	want := "/api/v1/memory/target/rollout?target_name=rke2-meho"
	if got != want {
		t.Errorf("buildRecallPath with target: got %q; want %q", got, want)
	}
}

func TestBuildRecallPathEscapesSlug(t *testing.T) {
	// SLUG_PATTERN admits letters/digits/hyphen/underscore/dot, all
	// of which url.PathEscape passes through. A space (which would
	// fail server-side validation but should still escape cleanly)
	// proves the encoding seam.
	got := buildRecallPath(ScopeUser, "weird slug", "")
	if !strings.Contains(got, "weird%20slug") {
		t.Errorf("expected escaped slug; got %q", got)
	}
}

func TestBuildListPathOmitsEmptyFilters(t *testing.T) {
	got := buildListPath(listOptions{})
	if got != "/api/v1/memory" {
		t.Errorf("expected bare path; got %q", got)
	}
}

func TestBuildListPathIncludesProvidedFilters(t *testing.T) {
	got := buildListPath(listOptions{
		ScopeArg:       "user-tenant",
		TagArg:         "rollout",
		SlugPatternArg: "wine",
		IncludeExpired: true,
		LimitArg:       50,
	})
	for _, want := range []string{
		"scope=user-tenant",
		"tag=rollout",
		"slug_pattern=wine",
		"include_expired=true",
		"limit=50",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("buildListPath missing %q; got %q", want, got)
		}
	}
}

func TestScopeFromKindStripsPrefix(t *testing.T) {
	if got := scopeFromKind("memory-user-tenant"); got != "user-tenant" {
		t.Errorf("scopeFromKind: got %q", got)
	}
	if got := scopeFromKind("kb-entry"); got != "kb-entry" {
		t.Errorf("non-memory kind should round-trip; got %q", got)
	}
}

func TestSlugFromSourceIDStripsSegments(t *testing.T) {
	// The source_id encoding the backend uses for memory rows is
	// `<scope>:<user_sub or target>:<slug>` for user/target-scoped
	// and `<scope>:<slug>` for tenant-scoped. The CLI's renderer
	// only needs the slug (the last segment).
	if got := slugFromSourceID("user-tenant:abc-def-123:wine-preference"); got != "wine-preference" {
		t.Errorf("slugFromSourceID 3-segment: got %q", got)
	}
	if got := slugFromSourceID("tenant:team-runbook"); got != "team-runbook" {
		t.Errorf("slugFromSourceID 2-segment: got %q", got)
	}
	if got := slugFromSourceID("plain"); got != "plain" {
		t.Errorf("slugFromSourceID no-segment: got %q", got)
	}
}

func TestPluralisePtrRendersNone(t *testing.T) {
	if got := pluralisePtr(nil); got != "(none)" {
		t.Errorf("nil pointer should render '(none)'; got %q", got)
	}
	empty := ""
	if got := pluralisePtr(&empty); got != "(none)" {
		t.Errorf("empty string should render '(none)'; got %q", got)
	}
	val := "set"
	if got := pluralisePtr(&val); got != "set" {
		t.Errorf("set value should render verbatim; got %q", got)
	}
}

// ---------------------------------------------------------------
// remember
// ---------------------------------------------------------------

func TestRunRememberHappyPath(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		expiresStr := "2026-05-26T12:00:00Z"
		_ = json.NewEncoder(w).Encode(Entry{
			ID:        "00000000-0000-0000-0000-000000000001",
			TenantID:  "00000000-0000-0000-0000-000000000002",
			Scope:     ScopeUserTenant,
			Slug:      "wine-preference",
			Body:      "I prefer dry red wines.",
			Metadata:  map[string]any{"tags": []string{"food", "pref"}},
			ExpiresAt: &expiresStr,
			CreatedAt: "2026-05-19T12:00:00Z",
			UpdatedAt: "2026-05-19T12:00:00Z",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRemember(cmd, rememberOptions{
		BodyArg:           "I prefer dry red wines.",
		ScopeArg:          "user-tenant",
		SlugArg:           "wine-preference",
		TagsArg:           []string{"food", "pref"},
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRemember: %v; stderr=%s", err, stderr.String())
	}
	if got := bodyJSON["scope"]; got != "user-tenant" {
		t.Errorf("expected scope in body; got %+v", bodyJSON)
	}
	if got := bodyJSON["body"]; got != "I prefer dry red wines." {
		t.Errorf("expected body in request; got %+v", bodyJSON)
	}
	if got := bodyJSON["slug"]; got != "wine-preference" {
		t.Errorf("expected slug in body; got %+v", bodyJSON)
	}
	md, ok := bodyJSON["metadata"].(map[string]any)
	if !ok {
		t.Fatalf("expected metadata map; got %T", bodyJSON["metadata"])
	}
	tags, ok := md["tags"].([]any)
	if !ok || len(tags) != 2 {
		t.Errorf("expected tags slice; got %+v", md)
	}
	if !strings.Contains(stdout.String(), "remembered user-tenant/wine-preference") {
		t.Errorf("expected success line; got %q", stdout.String())
	}
}

func TestRunRememberOmitsSlugWhenAbsent(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{Scope: ScopeUser, Slug: "auto-gen"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runRemember(cmd, rememberOptions{
		BodyArg: "body", ScopeArg: "user", BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runRemember: %v", err)
	}
	if _, ok := bodyJSON["slug"]; ok {
		t.Errorf("expected slug absent when not provided; got %+v", bodyJSON)
	}
	if _, ok := bodyJSON["metadata"]; ok {
		t.Errorf("expected metadata absent when no tags; got %+v", bodyJSON)
	}
}

func TestRunRememberJSONOutEmitsEntry(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{
			Scope: ScopeUserTenant, Slug: "x", Body: "y",
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runRemember(cmd, rememberOptions{
		BodyArg: "y", ScopeArg: "user-tenant", JSONOut: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runRemember --json: %v", err)
	}
	var decoded Entry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Slug != "x" || decoded.Body != "y" {
		t.Errorf("decoded: %+v", decoded)
	}
}

func TestRunRememberSendsExpiresAtFromTTL(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{Scope: ScopeUser, Slug: "x"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runRemember(cmd, rememberOptions{
		BodyArg: "y", ScopeArg: "user", TTLArg: "1d", BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runRemember: %v", err)
	}
	got, ok := bodyJSON["expires_at"].(string)
	if !ok || got == "" {
		t.Fatalf("expected expires_at in body; got %+v", bodyJSON)
	}
	// Sanity: parses as RFC3339 and is in the future.
	parsed, err := time.Parse(time.RFC3339, got)
	if err != nil {
		t.Fatalf("expires_at %q not RFC3339: %v", got, err)
	}
	if !parsed.After(time.Now()) {
		t.Errorf("expires_at %v should be in the future", parsed)
	}
}

func TestRunRememberTargetScopeRequiresTargetFlag(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRemember(cmd, rememberOptions{
		BodyArg: "y", ScopeArg: "target", BackplaneOverride: "https://meho.test",
	})
	if err == nil {
		t.Fatalf("expected error when --scope=target without --target")
	}
	if !strings.Contains(stderr.String(), "--target is required") {
		t.Errorf("expected target-required message; got %q", stderr.String())
	}
}

func TestRunRememberInvalidScopeFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRemember(cmd, rememberOptions{
		BodyArg: "y", ScopeArg: "bogus", BackplaneOverride: "https://meho.test",
	})
	if err == nil {
		t.Fatalf("expected error for invalid scope")
	}
	if !strings.Contains(stderr.String(), "invalid --scope") {
		t.Errorf("expected invalid-scope message; got %q", stderr.String())
	}
}

func TestRunRemember403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"permission_denied: role=operator cannot write scope=tenant"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRemember(cmd, rememberOptions{
		BodyArg: "y", ScopeArg: "tenant", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 403")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "permission_denied") {
		t.Errorf("expected detail to round-trip; got %q", stderr.String())
	}
	var ec interface{ ExitCode() int }
	if !errors.As(err, &ec) || ec.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

func TestRunRemember422SurfacesValidationDetail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":[{"loc":["body","slug"],"msg":"string does not match pattern"}]}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRemember(cmd, rememberOptions{
		BodyArg: "y", ScopeArg: "user", SlugArg: "BAD!",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 422")
	}
	if !strings.Contains(stderr.String(), "invalid request") {
		t.Errorf("expected invalid-request prefix; got %q", stderr.String())
	}
}

func TestRunRememberReadsBodyFromStdin(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(Entry{Scope: ScopeUser, Slug: "x", Body: "piped"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("piped body\n"))
	if err := runRemember(cmd, rememberOptions{
		BodyArg: "-", ScopeArg: "user", BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runRemember stdin: %v", err)
	}
	if got := bodyJSON["body"]; got != "piped body" {
		t.Errorf("expected piped body; got %+v", bodyJSON)
	}
}

// ---------------------------------------------------------------
// recall
// ---------------------------------------------------------------

func TestRunRecallByKeyPrintsBody(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user-tenant/wine-preference",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet {
				t.Errorf("expected GET; got %s", r.Method)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(Entry{
				Scope: ScopeUserTenant,
				Slug:  "wine-preference",
				Body:  "Prefers dry red.",
			})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRecall(cmd, recallOptions{
		ScopeSlugArg: "user-tenant/wine-preference", BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRecall: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "Prefers dry red.") {
		t.Errorf("expected body on stdout; got %q", stdout.String())
	}
}

func TestRunRecallByKey404SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/no-such",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"memory_not_found"}`)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRecall(cmd, recallOptions{
		ScopeSlugArg: "user/no-such", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 404")
	}
	if !strings.Contains(stderr.String(), "memory_not_found") {
		t.Errorf("expected memory_not_found in stderr; got %q", stderr.String())
	}
}

func TestRunRecallByKeyJSONOutEmitsEntry(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine",
		func(w http.ResponseWriter, _ *http.Request) {
			_ = json.NewEncoder(w).Encode(Entry{Scope: ScopeUser, Slug: "wine", Body: "y"})
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runRecall(cmd, recallOptions{
		ScopeSlugArg: "user/wine", JSONOut: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runRecall --json: %v", err)
	}
	var got Entry
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if got.Slug != "wine" {
		t.Errorf("decoded: %+v", got)
	}
}

func TestRunRecallTargetScopeRequiresTargetFlag(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRecall(cmd, recallOptions{
		ScopeSlugArg: "target/rollout", BackplaneOverride: "https://meho.test",
	})
	if err == nil {
		t.Fatalf("expected error when --scope=target without --target")
	}
	if !strings.Contains(stderr.String(), "--target is required") {
		t.Errorf("expected target-required message; got %q", stderr.String())
	}
}

func TestRunRecallRequiresExactlyOneMode(t *testing.T) {
	// Neither positional nor --query: error.
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runRecall(cmd, recallOptions{BackplaneOverride: "https://meho.test"}); err == nil {
		t.Fatalf("expected error with neither arg nor --query")
	}
	if !strings.Contains(stderr.String(), "exactly one") {
		t.Errorf("expected exactly-one message; got %q", stderr.String())
	}

	// Both positional and --query: error.
	cmd2, _, stderr2 := newRunCmd(t)
	cmd2.SetIn(bytes.NewBufferString(""))
	if err := runRecall(cmd2, recallOptions{
		ScopeSlugArg: "user/x", QueryArg: "wine",
		BackplaneOverride: "https://meho.test",
	}); err == nil {
		t.Fatalf("expected error with both arg and --query")
	}
	if !strings.Contains(stderr2.String(), "exactly one") {
		t.Errorf("expected exactly-one message; got %q", stderr2.String())
	}
}

func TestRunRecallByQueryHappyPath(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/retrieve", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &bodyJSON); err != nil {
			t.Fatalf("decode: %v", err)
		}
		_ = json.NewEncoder(w).Encode(RetrieveResponse{
			Hits: []RetrievalHit{
				{
					DocumentID: "1",
					Source:     "memory",
					SourceID:   "user-tenant:abc:wine",
					Kind:       "memory-user-tenant",
					Body:       "I prefer dry red wines.",
					FusedScore: 0.9,
				},
			},
			QueryDurationMS: 12.5,
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRecall(cmd, recallOptions{
		QueryArg:          "wine",
		ScopeFilterArg:    "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runRecall --query: %v; stderr=%s", err, stderr.String())
	}
	if got := bodyJSON["source"]; got != "memory" {
		t.Errorf("expected source=memory; got %+v", bodyJSON)
	}
	if got := bodyJSON["kind"]; got != "memory-user-tenant" {
		t.Errorf("expected kind=memory-user-tenant; got %+v", bodyJSON)
	}
	if got := bodyJSON["query"]; got != "wine" {
		t.Errorf("expected query forwarded; got %+v", bodyJSON)
	}
	if !strings.Contains(stdout.String(), "wine") {
		t.Errorf("expected slug 'wine' in rendered table; got %q", stdout.String())
	}
}

func TestRunRecallQueryRejectsBadLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runRecall(cmd, recallOptions{
		QueryArg: "x", LimitArg: 51, BackplaneOverride: "https://meho.test",
	})
	if err == nil {
		t.Fatalf("expected error for --limit=51")
	}
	if !strings.Contains(stderr.String(), "between 1 and 50") {
		t.Errorf("expected limit-range message; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// forget
// ---------------------------------------------------------------

func TestRunForgetHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user-tenant/wine-preference",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete {
				t.Errorf("expected DELETE; got %s", r.Method)
			}
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runForget(cmd, forgetOptions{
		ScopeSlugArg:      "user-tenant/wine-preference",
		Confirm:           true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runForget: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "forgot memory user-tenant/wine-preference") {
		t.Errorf("expected success line; got %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), "idempotent") {
		t.Errorf("expected idempotent hint; got %q", stdout.String())
	}
}

func TestRunForgetIdempotentOnMissing(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/gone",
		func(w http.ResponseWriter, _ *http.Request) {
			// Backend returns 204 whether or not the row existed.
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runForget(cmd, forgetOptions{
		ScopeSlugArg: "user/gone", Confirm: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runForget idempotent: %v", err)
	}
	if !strings.Contains(stdout.String(), "forgot") {
		t.Errorf("expected success line; got %q", stdout.String())
	}
}

func TestRunForgetDeclinedExits0WithoutBackend(t *testing.T) {
	called := 0
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine",
		func(w http.ResponseWriter, _ *http.Request) {
			called++
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("n\n")) // decline
	if err := runForget(cmd, forgetOptions{
		ScopeSlugArg: "user/wine", BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runForget declined: %v", err)
	}
	if called != 0 {
		t.Errorf("backend should not be called on decline; calls=%d", called)
	}
	if !strings.Contains(stdout.String(), "declined") {
		t.Errorf("expected declined hint; got %q", stdout.String())
	}
}

func TestRunForgetJSONOnDecline(t *testing.T) {
	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("\n")) // decline (default)
	if err := runForget(cmd, forgetOptions{
		ScopeSlugArg: "user/wine", JSONOut: true,
		BackplaneOverride: "https://meho.test",
	}); err != nil {
		t.Fatalf("runForget declined --json: %v", err)
	}
	// In --json mode the prompt is routed to stderr so the JSON
	// envelope on stdout stays parseable for `jq` consumers.
	var got forgetResult
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout not JSON: %v; stdout=%q stderr=%q", err, stdout.String(), stderr.String())
	}
	if got.Status != "declined" {
		t.Errorf("expected declined; got %+v", got)
	}
	if !strings.Contains(stderr.String(), "Forget memory") {
		t.Errorf("expected prompt on stderr in --json mode; got %q", stderr.String())
	}
}

func TestRunForgetRejectsTargetScopeWithoutTarget(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runForget(cmd, forgetOptions{
		ScopeSlugArg: "target/rollout", Confirm: true,
		BackplaneOverride: "https://meho.test",
	})
	if err == nil {
		t.Fatalf("expected error for target scope without --target")
	}
	if !strings.Contains(stderr.String(), "--target is required") {
		t.Errorf("expected target-required message; got %q", stderr.String())
	}
}

func TestRunForget403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/tenant/team-note",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"detail":"permission_denied: role=operator"}`)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runForget(cmd, forgetOptions{
		ScopeSlugArg: "tenant/team-note", Confirm: true, BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 403")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// list
// ---------------------------------------------------------------

func TestRunListHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ListResponse{
			Entries: []Entry{
				{Scope: ScopeUser, Slug: "wine", Body: "dry red"},
				{Scope: ScopeUserTenant, Slug: "k8s.note", Body: "rollout"},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"user", "wine", "user-tenant", "k8s.note"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in rendered table; got %q", want, out)
		}
	}
}

func TestRunListJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(ListResponse{
			Entries: []Entry{{Scope: ScopeUser, Slug: "x", Body: "y"}},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{
		JSONOut: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList --json: %v", err)
	}
	var resp ListResponse
	if err := json.Unmarshal(stdout.Bytes(), &resp); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(resp.Entries) != 1 || resp.Entries[0].Slug != "x" {
		t.Errorf("decoded: %+v", resp)
	}
}

func TestRunListEmptyResponse(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(ListResponse{Entries: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList empty: %v", err)
	}
	if !strings.Contains(stdout.String(), "no memories") {
		t.Errorf("expected empty hint; got %q", stdout.String())
	}
}

func TestRunListForwardsFilters(t *testing.T) {
	var capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		capturedQuery = r.URL.RawQuery
		_ = json.NewEncoder(w).Encode(ListResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{
		ScopeArg: "user-tenant", TagArg: "rollout",
		IncludeExpired:    true,
		LimitArg:          50,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList filters: %v", err)
	}
	for _, want := range []string{
		"scope=user-tenant", "tag=rollout", "include_expired=true", "limit=50",
	} {
		if !strings.Contains(capturedQuery, want) {
			t.Errorf("expected %q in query; got %q", want, capturedQuery)
		}
	}
}

func TestRunListNormalisesScopeWhitespace(t *testing.T) {
	// Whitespace-padded --scope must reach the backend trimmed.
	// Without normalisation runList passed parseScope's preflight
	// (validScopes lookup trims internally) and then forwarded the
	// raw " user " back into the query string, producing a 422 on
	// the FastAPI enum check. Mirrors the recall.go fix shape that
	// already propagates parseScope's typed return value.
	var capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		capturedQuery = r.URL.RawQuery
		_ = json.NewEncoder(w).Encode(ListResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{
		ScopeArg:          " user ",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList: %v", err)
	}
	if !strings.Contains(capturedQuery, "scope=user") {
		t.Errorf("expected trimmed scope in query; got %q", capturedQuery)
	}
	// Defensive: no URL-encoded space should leak through.
	for _, bad := range []string{"scope=%20user", "scope=user%20", "scope=+user", "scope=user+"} {
		if strings.Contains(capturedQuery, bad) {
			t.Errorf("query string %q still carries un-trimmed scope (%q)", capturedQuery, bad)
		}
	}
}

func TestRunListRejectsBadLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{
		LimitArg: 501, BackplaneOverride: "https://meho.test",
	}); err == nil {
		t.Fatalf("expected error for --limit=501")
	}
	if !strings.Contains(stderr.String(), "between 1 and 500") {
		t.Errorf("expected limit message; got %q", stderr.String())
	}
}

func TestRunListInvalidScopeFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runList(cmd, listOptions{
		ScopeArg: "bogus", BackplaneOverride: "https://meho.test",
	}); err == nil {
		t.Fatalf("expected error for invalid scope")
	}
	if !strings.Contains(stderr.String(), "invalid --scope") {
		t.Errorf("expected invalid-scope message; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// Cobra registration — every advertised verb must register.
// ---------------------------------------------------------------

func TestNewRememberCmdRegistersFlags(t *testing.T) {
	c := NewRememberCmd()
	for _, want := range []string{"scope", "slug", "target", "tag", "ttl", "json", "backplane"} {
		if c.Flags().Lookup(want) == nil {
			t.Errorf("remember missing flag --%s", want)
		}
	}
}

func TestNewRecallCmdRegistersFlags(t *testing.T) {
	c := NewRecallCmd()
	for _, want := range []string{"query", "scope", "limit", "target", "json", "backplane"} {
		if c.Flags().Lookup(want) == nil {
			t.Errorf("recall missing flag --%s", want)
		}
	}
}

func TestNewForgetCmdRegistersFlags(t *testing.T) {
	c := NewForgetCmd()
	for _, want := range []string{"confirm", "target", "json", "backplane"} {
		if c.Flags().Lookup(want) == nil {
			t.Errorf("forget missing flag --%s", want)
		}
	}
}

func TestNewListCmdRegistersFlags(t *testing.T) {
	c := NewListCmd()
	for _, want := range []string{
		"scope", "tag", "slug-pattern", "include-expired", "limit", "json", "backplane",
	} {
		if c.Flags().Lookup(want) == nil {
			t.Errorf("list missing flag --%s", want)
		}
	}
}

func TestNewRememberCmdHelpMentionsKeyFlags(t *testing.T) {
	c := NewRememberCmd()
	var buf bytes.Buffer
	c.SetOut(&buf)
	c.SetErr(&buf)
	c.SetArgs([]string{"--help"})
	if err := c.Execute(); err != nil {
		t.Fatalf("`meho remember --help`: %v", err)
	}
	help := buf.String()
	for _, want := range []string{"scope", "ttl", "tag", "target", "json"} {
		if !strings.Contains(help, want) {
			t.Errorf("expected help to mention %q; got:\n%s", want, help)
		}
	}
}
