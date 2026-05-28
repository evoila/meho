// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunPromoteHappyPath exercises the fresh-promotion branch end-to-
// end through the auth + transport stack. The httptest server returns
// a target row whose “created_at“ is *after* the pre-POST timestamp
// so isIdempotentRerun classifies the response as fresh (exit 0).
func TestRunPromoteHappyPath(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine-preference/promote",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &bodyJSON); err != nil {
				t.Fatalf("decode request: %v", err)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			// CreatedAt slightly in the future ensures
			// isIdempotentRerun returns false even if the test
			// runner's clock skews backward by milliseconds.
			created := time.Now().UTC().Add(50 * time.Millisecond)
			_ = json.NewEncoder(w).Encode(api.MemoryEntry{
				Id:        uuidFromString(t, "00000000-0000-0000-0000-000000000001"),
				TenantId:  uuidFromString(t, "00000000-0000-0000-0000-000000000002"),
				Scope:     ScopeUserTenant,
				Slug:      "wine-preference",
				Body:      "Prefers dry red.",
				Metadata:  map[string]interface{}{"promoted_from": "user/wine-preference"},
				CreatedAt: created,
				UpdatedAt: created,
			})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/wine-preference",
		ToArg:             "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runPromote: %v; stderr=%s", err, stderr.String())
	}
	if got := bodyJSON["to"]; got != "user-tenant" {
		t.Errorf("expected to=user-tenant in body; got %+v", bodyJSON)
	}
	if got := bodyJSON["move"]; got != false {
		t.Errorf("expected move=false default; got %+v", bodyJSON)
	}
	out := stdout.String()
	for _, want := range []string{"promoted user-tenant/wine-preference", "promoted_from:", "user/wine-preference"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in stdout; got %q", want, out)
		}
	}
	// Idempotent suffix must not appear on a fresh promotion.
	if strings.Contains(out, "already promoted") {
		t.Errorf("fresh promotion should not render idempotent wording; got %q", out)
	}
}

// TestRunPromoteMoveHappyPath asserts “--move“ lands on the wire and
// the human summary mentions source removal.
func TestRunPromoteMoveHappyPath(t *testing.T) {
	var bodyJSON map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/note/promote",
		func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &bodyJSON); err != nil {
				t.Fatalf("decode: %v", err)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			created := time.Now().UTC().Add(50 * time.Millisecond)
			_ = json.NewEncoder(w).Encode(api.MemoryEntry{
				Scope: ScopeUserTenant, Slug: "note", Body: "z",
				Metadata:  map[string]interface{}{"promoted_from": "user/note"},
				CreatedAt: created, UpdatedAt: created,
			})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/note",
		ToArg:             "user-tenant",
		Move:              true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runPromote --move: %v; stderr=%s", err, stderr.String())
	}
	if got := bodyJSON["move"]; got != true {
		t.Errorf("expected move=true in body; got %+v", bodyJSON)
	}
	if !strings.Contains(stdout.String(), "source row removed") {
		t.Errorf("expected source-removed suffix; got %q", stdout.String())
	}
}

// TestRunPromoteIdempotentRerunHumanExit6 — the server returns 200
// with an entry whose “created_at“ is well before the CLI's pre-POST
// timestamp. The CLI surfaces the no-op as exit 6 in human-readable
// mode.
func TestRunPromoteIdempotentRerunHumanExit6(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			// Created an hour ago — well before any pre-POST clock.
			ancient := time.Now().UTC().Add(-1 * time.Hour)
			_ = json.NewEncoder(w).Encode(api.MemoryEntry{
				Id:        uuidFromString(t, "11111111-1111-1111-1111-111111111111"),
				Scope:     ScopeUserTenant,
				Slug:      "wine",
				Body:      "prefers dry red",
				Metadata:  map[string]interface{}{"promoted_from": "user/wine"},
				CreatedAt: ancient,
				UpdatedAt: ancient,
			})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/wine",
		ToArg:             "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected idempotent-promote error (exit 6); got nil; stderr=%s", stderr.String())
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder; got %T (%v)", err, err)
	}
	if coder.ExitCode() != 6 {
		t.Errorf("expected exit 6 (idempotent); got %d", coder.ExitCode())
	}
	if !strings.Contains(stdout.String(), "already promoted") {
		t.Errorf("expected idempotent wording; got %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), errCodeIdempotentPromote) {
		t.Errorf("expected canonical code string; got %q", stdout.String())
	}
}

// TestRunPromoteIdempotentRerunJSONExit0 — under “--json“ the
// idempotent re-run collapses to exit 0 so scripts piping into “jq“
// don't trip on a successful no-op. The entry envelope is emitted
// verbatim.
func TestRunPromoteIdempotentRerunJSONExit0(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			ancient := time.Now().UTC().Add(-1 * time.Hour)
			_ = json.NewEncoder(w).Encode(api.MemoryEntry{
				Id:        uuidFromString(t, "22222222-2222-2222-2222-222222222222"),
				Scope:     ScopeUserTenant,
				Slug:      "wine",
				Body:      "prefers dry red",
				Metadata:  map[string]interface{}{"promoted_from": "user/wine"},
				CreatedAt: ancient,
				UpdatedAt: ancient,
			})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/wine",
		ToArg:             "user-tenant",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("--json idempotent should be exit 0; got %v; stderr=%s", err, stderr.String())
	}
	var decoded api.MemoryEntry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Id.String() != "22222222-2222-2222-2222-222222222222" {
		t.Errorf("expected target row id round-trip; got %+v", decoded)
	}
	// The promoted_from marker must survive the JSON round-trip so a
	// jq consumer can grep on it (#627 AC).
	got, _ := decoded.Metadata["promoted_from"].(string)
	if got != "user/wine" {
		t.Errorf("expected promoted_from in JSON envelope; got %+v", decoded.Metadata)
	}
}

// TestRunPromote403SurfacesInsufficientPromotionAuthority — the route
// returns 403 with the canonical
// “insufficient_promotion_authority“ detail; the CLI surfaces it as
// exit 5 with the detail verbatim per #627 AC.
func TestRunPromote403SurfacesInsufficientPromotionAuthority(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user-tenant/team-note/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"detail":"insufficient_promotion_authority"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user-tenant/team-note",
		ToArg:             "tenant",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 403")
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) || coder.ExitCode() != 5 {
		t.Errorf("expected exit 5 (insufficient_role); got %v", err)
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role code; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "insufficient_promotion_authority") {
		t.Errorf("expected promote detail round-trip; got %q", stderr.String())
	}
}

// TestRunPromote400CrossLadderSurfacesUnexpected — cross-ladder steps
// (e.g. “user-tenant -> target“) return 400 from the route; the CLI
// classes them as unexpected_response (exit 4) per #627 AC.
func TestRunPromote400CrossLadderSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user-tenant/team-note/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, `{"detail":"cross_ladder: user-tenant cannot promote to target"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user-tenant/team-note",
		ToArg:             "target",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 400")
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) || coder.ExitCode() != 4 {
		t.Errorf("expected exit 4 (unexpected_response); got %v", err)
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response code; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "cross_ladder") {
		t.Errorf("expected cross_ladder detail round-trip; got %q", stderr.String())
	}
}

// TestRunPromote404SourceNotVisibleSurfacesUnexpected — the route
// collapses "source not found" + "source not visible" into 404 for
// tenant-boundary info-leak avoidance; the CLI passes that through as
// unexpected_response (exit 4) per #627 AC.
func TestRunPromote404SourceNotVisibleSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/ghost/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"memory_not_found"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/ghost",
		ToArg:             "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 404")
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) || coder.ExitCode() != 4 {
		t.Errorf("expected exit 4 (unexpected_response); got %v", err)
	}
	if !strings.Contains(stderr.String(), "memory_not_found") {
		t.Errorf("expected memory_not_found detail round-trip; got %q", stderr.String())
	}
}

// TestRunPromoteUnreachableNetworkErrorSurfacesExit3 — a server-side
// abort surfaces as unreachable (exit 3).
func TestRunPromoteUnreachableNetworkErrorSurfacesExit3(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Close the connection without writing anything so the
		// client sees a transport error.
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Fatalf("http.ResponseWriter does not support Hijacker")
		}
		conn, _, err := hijacker.Hijack()
		if err != nil {
			t.Fatalf("hijack: %v", err)
		}
		_ = conn.Close()
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/foo",
		ToArg:             "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected transport error")
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) || coder.ExitCode() != 3 {
		t.Errorf("expected exit 3 (unreachable); got %v", err)
	}
	if !strings.Contains(stderr.String(), "unreachable") {
		t.Errorf("expected unreachable code; got %q", stderr.String())
	}
}

// TestRunPromote401AuthExpiredSurfacesExit2 — exhausting the refresh
// budget renders auth_expired (exit 2) with a `meho login` hint.
func TestRunPromote401AuthExpiredSurfacesExit2(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/x/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprint(w, `{"detail":"token expired"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL) // no refresh_token present

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/x",
		ToArg:             "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error from 401")
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) || coder.ExitCode() != 2 {
		t.Errorf("expected exit 2 (auth_expired); got %v", err)
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired code; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected meho login hint; got %q", stderr.String())
	}
}

// TestRunPromoteJSONHappyPathEmitsEntry — “--json“ on the fresh-
// promotion branch emits the raw MemoryEntry envelope; exit 0.
func TestRunPromoteJSONHappyPathEmitsEntry(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			created := time.Now().UTC().Add(50 * time.Millisecond)
			_ = json.NewEncoder(w).Encode(api.MemoryEntry{
				Id:        uuidFromString(t, "33333333-3333-3333-3333-333333333333"),
				Scope:     ScopeUserTenant,
				Slug:      "wine",
				Body:      "prefers dry red",
				Metadata:  map[string]interface{}{"promoted_from": "user/wine"},
				CreatedAt: created,
				UpdatedAt: created,
			})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/wine",
		ToArg:             "user-tenant",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runPromote --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded api.MemoryEntry
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded.Id.String() != "33333333-3333-3333-3333-333333333333" {
		t.Errorf("expected target row id round-trip; got %+v", decoded)
	}
	got, _ := decoded.Metadata["promoted_from"].(string)
	if got != "user/wine" {
		t.Errorf("expected promoted_from in JSON envelope; got %+v", decoded.Metadata)
	}
}

// TestRunPromoteRejectsMissingToFlag — empty --to argument fails fast
// client-side (no round-trip).
func TestRunPromoteRejectsMissingToFlag(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(_ http.ResponseWriter, r *http.Request) {
		t.Errorf("network call not expected; got %s %s", r.Method, r.URL)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/foo",
		ToArg:             "", // empty -- parseScope rejects
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for empty --to")
	}
	if !strings.Contains(stderr.String(), "invalid --scope") {
		t.Errorf("expected scope-validation message; got %q", stderr.String())
	}
}

// TestRunPromoteRejectsMalformedScopeSlug — the positional arg must be
// “<scope>/<slug>“; bare slug fails fast.
func TestRunPromoteRejectsMalformedScopeSlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "bare-slug-no-scope",
		ToArg:             "user-tenant",
		BackplaneOverride: "https://meho.test",
	})
	if err == nil {
		t.Fatalf("expected error for malformed arg")
	}
	if !strings.Contains(stderr.String(), "expected <scope>/<slug>") {
		t.Errorf("expected arg-shape hint; got %q", stderr.String())
	}
}

// TestPromoteRegisteredOnRoot — the `meho promote` verb appears under
// the root's subcommand list via NewPromoteCmd. Pinning this here so a
// future root-cmd refactor that accidentally drops the registration
// fails loudly (mirrors the registration-pinning tests on the kb /
// targets / audit verb trees).
func TestPromoteRegisteredOnRoot(t *testing.T) {
	cmd := NewPromoteCmd()
	if cmd.Use != "promote <scope>/<slug>" {
		t.Errorf("unexpected Use: %q", cmd.Use)
	}
	if !cmd.SilenceUsage {
		t.Error("expected SilenceUsage on promote")
	}
	if !cmd.SilenceErrors {
		t.Error("expected SilenceErrors on promote")
	}
	// Required flags surface client-side, not via a 422 round-trip.
	if got := cmd.Flag("to"); got == nil {
		t.Fatal("expected --to flag declared")
	}
	for _, flag := range []string{"to", "move", "json", "backplane"} {
		if got := cmd.Flag(flag); got == nil {
			t.Errorf("expected --%s flag declared", flag)
		}
	}
}

// TestIsIdempotentRerunVariousTimestamps exercises the timestamp
// classifier directly so any edge case in the parse fallback is
// covered without standing up a httptest server.
func TestIsIdempotentRerunVariousTimestamps(t *testing.T) {
	preCall := time.Date(2026, 5, 21, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		name  string
		entry *api.MemoryEntry
		want  bool
	}{
		{
			name:  "nil entry returns false",
			entry: nil,
			want:  false,
		},
		{
			name:  "zero created_at returns false",
			entry: &api.MemoryEntry{},
			want:  false,
		},
		{
			name:  "created_at strictly before pre-call wall clock is idempotent",
			entry: &api.MemoryEntry{CreatedAt: time.Date(2026, 5, 21, 11, 0, 0, 0, time.UTC)},
			want:  true,
		},
		{
			name:  "created_at after pre-call wall clock is fresh",
			entry: &api.MemoryEntry{CreatedAt: time.Date(2026, 5, 21, 13, 0, 0, 0, time.UTC)},
			want:  false,
		},
		{
			name:  "created_at equal to pre-call wall clock is fresh",
			entry: &api.MemoryEntry{CreatedAt: time.Date(2026, 5, 21, 12, 0, 0, 0, time.UTC)},
			want:  false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := isIdempotentRerun(tc.entry, preCall)
			if got != tc.want {
				t.Errorf("isIdempotentRerun: got %v; want %v", got, tc.want)
			}
		})
	}
}

// TestMetadataStringFieldEdgeCases covers nil / missing-key / non-
// string paths so the printed-summary loop never panics on an
// unexpected metadata shape.
func TestMetadataStringFieldEdgeCases(t *testing.T) {
	if got := metadataStringField(nil, "promoted_from"); got != "" {
		t.Errorf("nil metadata: got %q", got)
	}
	if got := metadataStringField(map[string]any{}, "promoted_from"); got != "" {
		t.Errorf("empty metadata: got %q", got)
	}
	if got := metadataStringField(map[string]any{"promoted_from": 42}, "promoted_from"); got != "" {
		t.Errorf("non-string field: got %q", got)
	}
	if got := metadataStringField(map[string]any{"promoted_from": "user/x"}, "promoted_from"); got != "user/x" {
		t.Errorf("string field: got %q", got)
	}
}

// TestRunPromote200WithoutPayloadSurfacesUnexpected pins the
// nil-guard on the promote path. Mirrors the kb sibling's
// post-iter-2 nil-guard pattern.
func TestRunPromote200WithoutPayloadSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/memory/user/wine/promote",
		func(w http.ResponseWriter, _ *http.Request) {
			// No Content-Type → JSON200 stays nil.
			_, _ = w.Write([]byte(`{"id":"00000000-0000-0000-0000-000000000099"}`))
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmd, promoteOptions{
		ScopeSlugArg:      "user/wine",
		ToArg:             "user-tenant",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected nil-guard error on missing payload")
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a memory entry payload") {
		t.Errorf("expected nil-guard message; got %q", stderr.String())
	}
}
