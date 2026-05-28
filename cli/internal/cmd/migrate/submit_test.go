// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

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
	"sync/atomic"
	"testing"
	"time"

	"charm.land/huh/v2/spinner"
	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/migrate"
)

func TestMain(m *testing.M) {
	// Override the spinner to run in accessible+discard mode so tests
	// don't fail when /dev/tty is unavailable in a headless environment.
	runSpinnerFn = func(sp *spinner.Spinner) error {
		return sp.WithAccessible(true).WithOutput(io.Discard).Run()
	}
	m.Run()
}

// seedXDGAndToken seeds a per-test XDG config dir + file-based token store
// that backplane.Resolve / postOne will read. Mirrors the same helper in
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

// newTestCmd returns a cobra.Command with captured stdout/stderr buffers.
func newTestCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	// Add the flags doSubmit expects (non-interactive is read inside postWithRetry).
	cmd.Flags().Bool("non-interactive", false, "")
	cmd.Flags().String("backplane", "", "")
	return cmd, &stdout, &stderr
}

// makePlan builds a minimal SubmitPlan for testing.
func makePlan(slug, scope, body, sha string) migrate.SubmitPlan {
	return migrate.SubmitPlan{
		File:  migrate.MemoryFile{Path: "/tmp/" + slug + ".md", Type: "user", Body: body, BodySHA256: sha},
		Scope: scope,
		Slug:  slug,
		Body:  body,
	}
}

// stubMemoryEntry returns a JSON body matching the api.MemoryEntry
// shape (the 201 response payload). Used by httptest handlers so the
// generated ParseRememberApiV1MemoryPostResponse populates JSON201
// and postOne's nil-guard passes.
func stubMemoryEntry(t *testing.T, scope, slug string) []byte {
	t.Helper()
	now := time.Now().UTC()
	entry := api.MemoryEntry{
		Id:        uuid.New(),
		TenantId:  uuid.New(),
		Scope:     api.MemoryScope(scope),
		Slug:      slug,
		Body:      "stub",
		Metadata:  map[string]any{"tags": []string{}},
		CreatedAt: now,
		UpdatedAt: now,
	}
	raw, err := json.Marshal(entry)
	if err != nil {
		t.Fatalf("marshal stub entry: %v", err)
	}
	return raw
}

// ── POST body shape ───────────────────────────────────────────────────────────

func TestSubmit_PostsCorrectBody(t *testing.T) {
	var received []api.RememberBody
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		var b api.RememberBody
		if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
			t.Errorf("decode body: %v", err)
		}
		received = append(received, b)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(stubMemoryEntry(t, string(b.Scope), *b.Slug))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plans := []migrate.SubmitPlan{
		makePlan("daily-routine", "user", "I start my day by checking email.", "abcdef012345"),
		makePlan("code-style", "user", "Prefer explicit error handling.", "fedcba987654"),
	}

	cmd, _, _ := newTestCmd(t)
	if err := doSubmit(cmd, srv.URL, plans); err != nil {
		t.Fatalf("doSubmit: %v", err)
	}

	if len(received) != 2 {
		t.Fatalf("expected 2 POST calls, got %d", len(received))
	}
	for i, got := range received {
		want := plans[i]
		if string(got.Scope) != want.Scope {
			t.Errorf("[%d] scope = %q; want %q", i, string(got.Scope), want.Scope)
		}
		if got.Slug == nil || *got.Slug != want.Slug {
			t.Errorf("[%d] slug = %v; want %q", i, got.Slug, want.Slug)
		}
		if got.Body != want.Body {
			t.Errorf("[%d] body = %q; want %q", i, got.Body, want.Body)
		}
		if got.Metadata == nil {
			t.Errorf("[%d] metadata should not be nil", i)
		}
	}
}

// ── source_id stability (now a server-side property) ─────────────────────────

// The pre-G0.12-T11 CLI sent a `source_id` field in the body that the
// backend's frozen extra="forbid" RememberBody rejected with 422. The
// migration drops the field from the wire body — the backend computes
// source_id itself from `(scope, user_sub, target_name, slug)`
// (`backend/src/meho_backplane/memory/_internal.py::encode_source_id`)
// so the (scope, slug) pair is the deduplication key from the
// operator's perspective. Test that re-submitting an unchanged
// (scope, slug) entry hits the route twice without changing the body
// shape — the server-side upsert (G5.1) is what carries the
// "idempotent re-run" property the legacy CLI tried to express via
// source_id.

func TestSubmit_SameSlugRerunSendsSameBody(t *testing.T) {
	var received []api.RememberBody
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		var b api.RememberBody
		if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
			t.Errorf("decode body: %v", err)
		}
		received = append(received, b)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(stubMemoryEntry(t, string(b.Scope), *b.Slug))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "user", "same body", "aabbccdd1122")
	cmd, stdout, _ := newTestCmd(t)

	if err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan}); err != nil {
		t.Fatalf("first run: %v", err)
	}
	stdout.Reset()
	if err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan}); err != nil {
		t.Fatalf("second run: %v", err)
	}
	if len(received) != 2 {
		t.Fatalf("expected exactly 2 POST calls (one per run); got %d", len(received))
	}
	if string(received[0].Scope) != string(received[1].Scope) ||
		*received[0].Slug != *received[1].Slug ||
		received[0].Body != received[1].Body {
		t.Errorf("body shape drifted between identical reruns: %+v vs %+v", received[0], received[1])
	}
}

// ── No source_id on the wire ─────────────────────────────────────────────────

// Belt-and-suspenders: assert the typed body the CLI sends doesn't
// carry a `source_id` field. The generated RememberBody type has no
// such field, so this can only fail if a future regression adds one.

func TestSubmit_BodyOmitsSourceID(t *testing.T) {
	var rawBody []byte
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		rawBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(stubMemoryEntry(t, "user", "foo"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "user", "body", "aabbccdd1122")
	cmd, _, _ := newTestCmd(t)
	if err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan}); err != nil {
		t.Fatalf("doSubmit: %v", err)
	}
	var anyMap map[string]any
	if err := json.Unmarshal(rawBody, &anyMap); err != nil {
		t.Fatalf("decode raw body: %v", err)
	}
	if _, ok := anyMap["source_id"]; ok {
		t.Errorf("wire body must NOT contain source_id (the backend computes it); got: %s", rawBody)
	}
}

// ── Transient error retry ─────────────────────────────────────────────────────

func TestSubmit_TransientRetryThenSuccess(t *testing.T) {
	var callCount atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		n := callCount.Add(1)
		if n < 2 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(stubMemoryEntry(t, "user", "retry-me"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("retry-me", "user", "text", "abcdef012345")
	cmd, stdout, _ := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	if err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan}); err != nil {
		t.Fatalf("doSubmit: %v", err)
	}
	if callCount.Load() < 2 {
		t.Errorf("expected ≥2 calls (1 retry+1 success); got %d", callCount.Load())
	}
	if !strings.Contains(stdout.String(), "Migrated:") {
		t.Errorf("expected summary line in stdout, got: %q", stdout.String())
	}
}

// ── Summary line format ───────────────────────────────────────────────────────

func TestSubmit_SummaryLine(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		var b api.RememberBody
		_ = json.NewDecoder(r.Body).Decode(&b)
		slug := "a"
		if b.Slug != nil {
			slug = *b.Slug
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(stubMemoryEntry(t, string(b.Scope), slug))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plans := []migrate.SubmitPlan{
		makePlan("a", "user", "body a", "aaaaaaaaaaaa"),
		makePlan("b", "user", "body b", "bbbbbbbbbbbb"),
	}
	cmd, stdout, _ := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	if err := doSubmit(cmd, srv.URL, plans); err != nil {
		t.Fatalf("doSubmit: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, "Migrated: 2") {
		t.Errorf("expected 'Migrated: 2' in output, got: %q", out)
	}
	if !strings.Contains(out, "Skipped: 0") {
		t.Errorf("expected 'Skipped: 0' in output, got: %q", out)
	}
}

// ── isTransient ───────────────────────────────────────────────────────────────

func TestIsTransient_ServerErrors(t *testing.T) {
	for _, code := range []int{500, 502, 503, 504} {
		if !isTransient(&transientStatusError{StatusCode: code}) {
			t.Errorf("expected isTransient(%d)=true", code)
		}
	}
}

func TestIsTransient_ClientErrors(t *testing.T) {
	for _, code := range []int{400, 401, 403, 404, 422} {
		if isTransient(&transientStatusError{StatusCode: code}) {
			t.Errorf("expected isTransient(%d)=false", code)
		}
	}
}

func TestIsTransient_TransportError(t *testing.T) {
	if !isTransient(errors.New("connection refused")) {
		t.Error("expected isTransient(transport error)=true")
	}
}

// TestIsTransient_CredentialFailuresAreNonTransient is the unit-level
// guard for the B1 regression on PR #1285. Before the fix, the migration
// moved the auth-token resolution inside the retry loop; isTransient's
// generic "transport error → retry" fallthrough then classified
// errMissingAccessToken / auth.ErrTokenNotFound / errNoRefreshToken as
// transient, triggering up-to-3 wasted retries (non-interactive) or 3
// spurious Retry/Skip/Abort prompts (interactive) for an error that
// retrying CANNOT resolve — the operator must run `meho login`.
//
// Pin each of the three credential sentinels renderSubmitError already
// maps to auth_expired so the retry-vs-permanent decision agrees with
// the exit-code classification.
func TestIsTransient_CredentialFailuresAreNonTransient(t *testing.T) {
	cases := []struct {
		name string
		err  error
	}{
		{"errMissingAccessToken", errMissingAccessToken},
		{"errMissingAccessToken wrapped", fmt.Errorf("postOne: %w", errMissingAccessToken)},
		{"auth.ErrTokenNotFound", auth.ErrTokenNotFound},
		{"auth.ErrTokenNotFound wrapped", fmt.Errorf("api.NewAuthedClient: %w", auth.ErrTokenNotFound)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if isTransient(tc.err) {
				t.Errorf("expected isTransient(%v)=false (credential failures are not retryable)", tc.err)
			}
		})
	}
}

// TestSubmit_MissingTokenExitsOnceNoRetries is the end-to-end guard
// for the B1 regression on PR #1285. With XDG_CONFIG_HOME pointing at
// an empty dir, api.NewAuthedClient surfaces auth.ErrTokenNotFound;
// the fix routes that straight to renderSubmitError, so the operator
// sees a single auth_expired hint with zero HTTP calls and zero
// retries. Before the fix, isTransient classified it as transient and
// postWithRetry burned three retries (non-interactive) before giving
// up.
func TestSubmit_MissingTokenExitsOnceNoRetries(t *testing.T) {
	var serverCalls atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		serverCalls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(stubMemoryEntry(t, "user", "foo"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	// Empty XDG dir + keyring disabled → auth.ErrTokenNotFound from
	// api.NewAuthedClient. Mirrors the same env shape the production
	// "operator never ran `meho login`" path produces.
	xdg := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", xdg)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")

	plan := makePlan("foo", "user", "body", "aabbccdd1122")
	cmd, stdout, stderr := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan})
	if err == nil {
		t.Fatalf("expected error when token store is empty; got nil. stdout=%q stderr=%q",
			stdout.String(), stderr.String())
	}
	if got := serverCalls.Load(); got != 0 {
		t.Errorf("expected zero HTTP calls (credential failure short-circuits before POST); got %d", got)
	}
	if !strings.Contains(stderr.String(), "auth_expired") &&
		!strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected auth_expired classification or `meho login` hint; got stderr=%q", stderr.String())
	}
	// The summary line is printed by doSubmit before renderSubmitError
	// fires; it MUST show zero retries because credential failures are
	// non-transient and postWithRetry must NOT enter the retry path.
	if !strings.Contains(stdout.String(), "retried 0") {
		t.Errorf("expected 'retried 0' in summary (no retry attempts on credential failure); got stdout=%q", stdout.String())
	}
}

// ── 201 without JSON payload (Content-Type drift / nil-guard) ────────────────

// Belt-and-suspenders for the M2/M3-class punch-list finding from
// sibling T9 (#1282): the generated parser only populates JSON201 when
// the response has Content-Type containing "json" AND status 201. A
// 201 without that Content-Type leaves JSON201 nil; postOne MUST
// surface the contract failure rather than count it as a successful
// migration.
func TestSubmit_Rejects201WithoutJSONPayload(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		// 201 + non-JSON Content-Type → parser leaves JSON201 nil.
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte("OK"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "user", "body", "aabbccdd1122")
	cmd, stdout, stderr := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan})
	if err == nil {
		t.Fatalf("expected error on 201 without JSON payload; got nil. stdout=%q stderr=%q",
			stdout.String(), stderr.String())
	}
	if !strings.Contains(stderr.String(), "HTTP 201 without a memory entry payload") &&
		!strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification mentioning the 201-without-payload origin; got stderr=%q", stderr.String())
	}
}

// ── 401 surfaces as auth_expired ──────────────────────────────────────────────

func TestSubmit_401SurfacesAsAuthExpired(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":"token expired"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "user", "body", "aabbccdd1122")
	cmd, _, stderr := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan})
	if err == nil {
		t.Fatal("expected error on 401; got nil")
	}
	if !strings.Contains(stderr.String(), "auth_expired") &&
		!strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected auth_expired classification or `meho login` hint; got stderr=%q", stderr.String())
	}
}

// ── 403 surfaces as insufficient_role ─────────────────────────────────────────

func TestSubmit_403SurfacesAsInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"tenant_admin required to write tenant scope"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "tenant", "body", "aabbccdd1122")
	cmd, _, stderr := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan})
	if err == nil {
		t.Fatal("expected error on 403; got nil")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") &&
		!strings.Contains(stderr.String(), "tenant_admin required") {
		t.Errorf("expected insufficient_role classification surfacing backend detail; got stderr=%q", stderr.String())
	}
}

// ── 422 surfaces as unexpected_response (non-transient) ──────────────────────

func TestSubmit_422SurfacesAsUnexpectedNonTransient(t *testing.T) {
	var calls atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"unknown field 'source_id'"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "user", "body", "aabbccdd1122")
	cmd, _, stderr := newTestCmd(t)
	if err := cmd.Flags().Set("non-interactive", "true"); err != nil {
		t.Fatal(err)
	}

	err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan})
	if err == nil {
		t.Fatal("expected error on 422; got nil")
	}
	if calls.Load() != 1 {
		t.Errorf("expected exactly 1 call (422 is non-transient, no retries); got %d", calls.Load())
	}
	if !strings.Contains(stderr.String(), "unexpected_response") &&
		!strings.Contains(stderr.String(), "HTTP 422") {
		t.Errorf("expected unexpected_response with HTTP 422; got stderr=%q", stderr.String())
	}
}

// ── No backplane configured surfaces as auth_expired ─────────────────────────

func TestSubmit_NoBackplaneConfiguredSurfacesAsAuthExpired(t *testing.T) {
	// Empty XDG_CONFIG_HOME with no override means backplane.Resolve
	// returns *NotConfiguredError → ClassifyError → AuthExpired.
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)

	plan := makePlan("foo", "user", "body", "aabbccdd1122")
	cmd, _, stderr := newTestCmd(t)

	err := doSubmit(cmd, "", []migrate.SubmitPlan{plan})
	if err == nil {
		t.Fatal("expected error when no backplane is configured; got nil")
	}
	if !strings.Contains(stderr.String(), "auth_expired") &&
		!strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected auth_expired / `meho login` hint; got stderr=%q", stderr.String())
	}
}

// ── --mark-migrated ───────────────────────────────────────────────────────────

func TestMarkMigrated_WritesMarker(t *testing.T) {
	dir := t.TempDir()
	markerBase := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", markerBase)

	writeFixture(t, dir, "user.md", userFixture)

	called := false
	fn := func(_ []migrate.SubmitPlan) error { called = true; return nil }

	_, _, err := runMemory(t, []string{"--source", dir, "--non-interactive", "--mark-migrated"}, fn)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !called {
		t.Error("submitFn should have been called")
	}

	exists, err := migrate.MarkerExists(dir)
	if err != nil {
		t.Fatalf("MarkerExists: %v", err)
	}
	if !exists {
		t.Error("marker should exist after --mark-migrated")
	}
}

// ── buildRememberBody ────────────────────────────────────────────────────────

// Belt-and-suspenders: the typed body the CLI builds carries the
// operator's chosen scope verbatim through api.MemoryScope (no
// silent coercion to a different enum constant on a backend rename),
// stamps a non-nil pointer-slug so the FastAPI route's
// `slug: str | None` arm accepts the value, and writes a non-nil
// metadata pointer so the empty `tags` array reaches the wire.

func TestBuildRememberBody_ShapePreserved(t *testing.T) {
	plan := makePlan("daily-routine", "user", "I start my day by checking email.", "abc123")
	body := buildRememberBody(plan)
	if string(body.Scope) != "user" {
		t.Errorf("scope = %q; want %q", string(body.Scope), "user")
	}
	if body.Slug == nil || *body.Slug != "daily-routine" {
		t.Errorf("slug = %v; want %q", body.Slug, "daily-routine")
	}
	if body.Body != "I start my day by checking email." {
		t.Errorf("body = %q; want %q", body.Body, "I start my day by checking email.")
	}
	if body.Metadata == nil {
		t.Fatal("metadata pointer should not be nil")
	}
	tags, ok := (*body.Metadata)["tags"]
	if !ok {
		t.Fatal("metadata should carry 'tags' key")
	}
	if tagsSlice, ok := tags.([]string); !ok || len(tagsSlice) != 0 {
		t.Errorf("expected empty []string tags, got %T(%v)", tags, tags)
	}
}
