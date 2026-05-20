// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"charm.land/huh/v2/spinner"
	"github.com/spf13/cobra"

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
// that resolveBackplaneURL / doAuthedRequest will read. Mirrors the same
// helper in cli/internal/cmd/kb/kb_test.go.
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

// ── POST body shape ───────────────────────────────────────────────────────────

func TestSubmit_PostsCorrectBody(t *testing.T) {
	var received []memoryPostBody
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		var b memoryPostBody
		if err := json.NewDecoder(r.Body).Decode(&b); err != nil {
			t.Errorf("decode body: %v", err)
		}
		received = append(received, b)
		w.WriteHeader(http.StatusCreated)
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
		if got.Scope != want.Scope {
			t.Errorf("[%d] scope = %q; want %q", i, got.Scope, want.Scope)
		}
		if got.Slug != want.Slug {
			t.Errorf("[%d] slug = %q; want %q", i, got.Slug, want.Slug)
		}
		if got.Body != want.Body {
			t.Errorf("[%d] body = %q; want %q", i, got.Body, want.Body)
		}
		wantSourceID := "laptop-migration/" + want.File.BodySHA256[:migrate.SourceIDPrefix]
		if got.SourceID != wantSourceID {
			t.Errorf("[%d] source_id = %q; want %q", i, got.SourceID, wantSourceID)
		}
		if got.Metadata == nil {
			t.Errorf("[%d] metadata should not be nil", i)
		}
	}
}

// ── source_id stability / upsert contract ─────────────────────────────────────

func TestSubmit_SourceIDStable(t *testing.T) {
	p1 := makePlan("foo", "user", "same body", "aabbccdd1122")
	p2 := makePlan("foo", "user", "same body", "aabbccdd1122")
	id1 := buildSourceID(p1)
	id2 := buildSourceID(p2)
	if id1 != id2 {
		t.Errorf("source_id not stable: %q != %q", id1, id2)
	}
	if id1 != "laptop-migration/aabbccdd1122" {
		t.Errorf("source_id = %q; want laptop-migration/aabbccdd1122", id1)
	}
}

func TestSubmit_ChangedBody_DifferentSourceID(t *testing.T) {
	p1 := makePlan("foo", "user", "body v1", "aabbccdd1122")
	p2 := makePlan("foo", "user", "body v2", "112233aabbcc")
	if buildSourceID(p1) == buildSourceID(p2) {
		t.Error("expected different source_id for different BodySHA256")
	}
}

// TestSubmit_SameBodyRerun simulates the server-side upsert: two POSTs
// with the same source_id both succeed (server returns 200/201 either way).
func TestSubmit_SameBodyRerun(t *testing.T) {
	var callCount atomic.Int32
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, r *http.Request) {
		callCount.Add(1)
		w.WriteHeader(http.StatusCreated)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	plan := makePlan("foo", "user", "same body", "aabbccdd1122")
	cmd, stdout, _ := newTestCmd(t)

	// First run.
	if err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan}); err != nil {
		t.Fatalf("first run: %v", err)
	}
	// Second run with identical body.
	stdout.Reset()
	if err := doSubmit(cmd, srv.URL, []migrate.SubmitPlan{plan}); err != nil {
		t.Fatalf("second run: %v", err)
	}
	if callCount.Load() != 2 {
		t.Errorf("expected exactly 2 POST calls (one per run); got %d", callCount.Load())
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
		w.WriteHeader(http.StatusCreated)
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
	mux.HandleFunc("/api/v1/memory", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
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
		if !isTransient(&httpError{StatusCode: code}) {
			t.Errorf("expected isTransient(%d)=true", code)
		}
	}
}

func TestIsTransient_ClientErrors(t *testing.T) {
	for _, code := range []int{400, 401, 403, 404, 422} {
		if isTransient(&httpError{StatusCode: code}) {
			t.Errorf("expected isTransient(%d)=false", code)
		}
	}
}

// ── normaliseURL ──────────────────────────────────────────────────────────────

func TestNormaliseURL(t *testing.T) {
	cases := []struct {
		input   string
		want    string
		wantErr bool
	}{
		{"https://meho.example.com/", "https://meho.example.com", false},
		{"https://meho.example.com/api", "https://meho.example.com/api", false},
		{"", "", true},
		{"not-a-url", "", true},
		{"/just/a/path", "", true},
	}
	for _, tc := range cases {
		got, err := normaliseURL(tc.input)
		if tc.wantErr {
			if err == nil {
				t.Errorf("normaliseURL(%q): expected error, got nil", tc.input)
			}
		} else {
			if err != nil {
				t.Errorf("normaliseURL(%q): unexpected error %v", tc.input, err)
			}
			if got != tc.want {
				t.Errorf("normaliseURL(%q) = %q; want %q", tc.input, got, tc.want)
			}
		}
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
