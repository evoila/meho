// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestPrintListTableEmpty — zero-entry tenant renders the no-entries
// line without the header row.
func TestPrintListTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, &api.KbListResponse{Entries: nil})
	out := buf.String()
	if !strings.Contains(out, "no kb entries") {
		t.Errorf("empty render missing hint; got %q", out)
	}
	if strings.Contains(out, "SLUG") {
		t.Errorf("empty render should skip header; got %q", out)
	}
}

// TestPrintListTableRendersColumns — header + every column appears.
func TestPrintListTableRendersColumns(t *testing.T) {
	resp := &api.KbListResponse{
		Entries: []api.KbEntryPreview{
			{
				Slug:      "vcenter-9.0-overview",
				Preview:   "vCenter 9.0 has new APIs for…",
				CreatedAt: "2026-05-01T00:00:00Z",
				UpdatedAt: "2026-05-12T10:11:12Z",
				Metadata:  map[string]any{"owner": "ops"},
			},
		},
	}
	var buf bytes.Buffer
	printListTable(&buf, resp)
	out := buf.String()
	for _, want := range []string{"SLUG", "UPDATED", "PREVIEW", "vcenter-9.0-overview", "2026-05-12T10:11:12Z", "vCenter 9.0 has new APIs"} {
		if !strings.Contains(out, want) {
			t.Errorf("printListTable missing %q in %q", want, out)
		}
	}
}

// TestPrintListTablePreservesFullTimestamp — the docstring on
// printListTable promises operators correlating with audit-log
// rows that the full ISO-8601 `updated_at` is rendered intact.
// Python's `datetime.isoformat()` with microseconds + offset
// produces a 32-char string (`YYYY-MM-DDTHH:MM:SS.ffffff+HH:MM`),
// and the UPDATED column must accommodate it without truncation.
// The generated `api.KbEntryPreview.UpdatedAt` is a `string` (the
// backend serialises the timestamp via `datetime.isoformat()`),
// so the column-width contract survives the typed-client migration
// unchanged.
func TestPrintListTablePreservesFullTimestamp(t *testing.T) {
	const fullTimestamp = "2026-05-12T10:11:12.123456+00:00" // 32 chars
	if got := len(fullTimestamp); got != 32 {
		t.Fatalf("fixture length: got %d; want 32 (test setup error)", got)
	}
	resp := &api.KbListResponse{
		Entries: []api.KbEntryPreview{
			{
				Slug:      "vcenter-9.0-overview",
				Preview:   "preview",
				UpdatedAt: fullTimestamp,
				Metadata:  map[string]any{},
			},
		},
	}
	var buf bytes.Buffer
	printListTable(&buf, resp)
	if !strings.Contains(buf.String(), fullTimestamp) {
		t.Errorf("expected full 32-char timestamp intact in render; got %q", buf.String())
	}
}

// TestRunListRejectsOutOfRangeLimit — --limit > 500 is caught at
// the CLI before the round-trip.
func TestRunListRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{Limit: 501})
	if err == nil {
		t.Fatalf("expected error for over-budget --limit")
	}
	if !strings.Contains(stderr.String(), "between 1 and 500") {
		t.Errorf("stderr missing range hint; got %q", stderr.String())
	}
}

// TestRunListRejectsNegativeLimit — symmetrical case for --limit=-1.
func TestRunListRejectsNegativeLimit(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	err := runList(cmd, listOptions{Limit: -1})
	if err == nil {
		t.Fatalf("expected error for negative --limit")
	}
}

// TestRunListRejectsNegativeOffset — same gate on --offset.
func TestRunListRejectsNegativeOffset(t *testing.T) {
	cmd, _, _ := newRunCmd(t)
	err := runList(cmd, listOptions{Offset: -1})
	if err == nil {
		t.Fatalf("expected error for negative --offset")
	}
}

// TestRunListHappyPath drives runList end-to-end through the typed
// client transport against an httptest server. The wire shape
// (query params, Authorization header) is built by the generated
// `api.ListKbApiV1KbGetWithResponse`; the test asserts on the
// server-observed query string to pin the migration's contract.
func TestRunListHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("filter"); got != "vcenter%" {
			t.Errorf("query filter: got %q; want %q", got, "vcenter%")
		}
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbListResponse{
			Entries: []api.KbEntryPreview{
				{
					Slug:      "vcenter-9.0-overview",
					Preview:   "vCenter 9.0 has…",
					CreatedAt: "2026-05-01T00:00:00Z",
					UpdatedAt: "2026-05-12T10:11:12Z",
					Metadata:  map[string]any{},
				},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{Filter: "vcenter%", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{"SLUG", "vcenter-9.0-overview"} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in %q", want, out)
		}
	}
}

// TestRunListJSONHappyPath — --json round-trips the raw response
// shape; operators piping through jq get a stable contract.
func TestRunListJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbListResponse{
			Entries: []api.KbEntryPreview{
				{Slug: "a", Preview: "p", CreatedAt: "t", UpdatedAt: "u", Metadata: map[string]any{}},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runList --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded api.KbListResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded.Entries) != 1 || decoded.Entries[0].Slug != "a" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestRunList401SurfacesAuthExpired — exhausting the refresh budget
// (no refresh_token present) must render as auth_expired with the
// `meho login` hint. The typed-client equivalent of the
// pre-migration httpError-401 path: retryOn401 invokes
// `authed.Refresh`, which returns errNoRefreshToken, and
// renderRequestError maps that to auth_expired.
func TestRunList401SurfacesAuthExpired(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		fmt.Fprint(w, `{"detail":"token expired"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error; stdout=%s", stdout.String())
	}
	if !strings.Contains(stderr.String(), "auth_expired") {
		t.Errorf("expected auth_expired in stderr; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "meho login") {
		t.Errorf("expected `meho login` hint; got %q", stderr.String())
	}
}

// TestRunList403SurfacesInsufficientRole — RBAC denial renders with
// the required-role string the backend supplied and exits 5.
func TestRunList403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: operator required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunListWiresFilterLimitOffsetOnWire pins the generated
// client's query-param serialisation. The typed
// ListKbApiV1KbGetParams pointers land as the documented `filter`,
// `limit`, and `offset` keys on the wire. The pre-migration test
// covered the URL-build helper directly; this replacement asserts
// the same property end-to-end through the call site.
func TestRunListWiresFilterLimitOffsetOnWire(t *testing.T) {
	var seenQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		seenQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbListResponse{Entries: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{
		Filter: "vcenter%", Limit: 25, Offset: 10, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"filter=vcenter%25", "limit=25", "offset=10"} {
		if !strings.Contains(seenQuery, want) {
			t.Errorf("expected %q in query string; got %q", want, seenQuery)
		}
	}
}

// TestRunListOmitsZeroLimitOffsetOnWire pins the zero-value query-
// omission contract (m1). `listQueryParams` deliberately omits the
// `limit` and `offset` keys when the operator didn't pass the
// flag so the backend's `Query(ge=1, le=500, default=100)` and
// `Query(ge=0, default=0)` apply server-side. A regression that
// flipped `if opts.Limit > 0` to `if opts.Limit >= 0` would
// silently start sending `limit=0`, which the backend's `ge=1`
// constraint rejects as a 422 — the contract test is the only
// thing that catches it. Restored after the migration dropped the
// pre-migration equivalent.
func TestRunListOmitsZeroLimitOffsetOnWire(t *testing.T) {
	var seenQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		seenQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.KbListResponse{Entries: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{
		Limit: 0, Offset: 0, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, forbidden := range []string{"limit=", "offset="} {
		if strings.Contains(seenQuery, forbidden) {
			t.Errorf("expected %q absent from query; got %q", forbidden, seenQuery)
		}
	}
}

// TestRunListRejects200WithoutJSONPayload pins the JSON200
// nil-guard (M6). A 200 with a missing or mistyped Content-Type
// leaves resp.JSON200 nil; without the guard, printListTable
// prints "no kb entries registered in this tenant" — actively
// misleading (conflated with a genuinely-empty tenant). Route to
// output.Unexpected (exit 4) instead.
func TestRunListRejects200WithoutJSONPayload(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		// Deliberately omit Content-Type so the generated parser
		// leaves JSON200 nil.
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("not-json"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 200 without JSON payload")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a kb list payload") {
		t.Errorf("expected detail mentioning missing payload; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}
