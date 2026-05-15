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
)

// TestBuildListPathEmpty — empty options yield a clean path with
// no query string.
func TestBuildListPathEmpty(t *testing.T) {
	if got := buildListPath(listOptions{}); got != "/api/v1/kb" {
		t.Fatalf("empty opts path: got %q; want %q", got, "/api/v1/kb")
	}
}

// TestBuildListPathSetsAllFilters — every option lands on the wire.
func TestBuildListPathSetsAllFilters(t *testing.T) {
	got := buildListPath(listOptions{Filter: "vcenter%", Limit: 25, Offset: 10})
	for _, want := range []string{"filter=vcenter%25", "limit=25", "offset=10"} {
		if !strings.Contains(got, want) {
			t.Errorf("buildListPath missing %q in %q", want, got)
		}
	}
}

// TestBuildListPathOmitsZeroOffsetAndLimit — zero values stay off
// the wire so the backend's defaults apply.
func TestBuildListPathOmitsZeroOffsetAndLimit(t *testing.T) {
	got := buildListPath(listOptions{Filter: "x"})
	if strings.Contains(got, "limit=") {
		t.Errorf("expected limit omitted; got %q", got)
	}
	if strings.Contains(got, "offset=") {
		t.Errorf("expected offset omitted; got %q", got)
	}
}

// TestPrintListTableEmpty — zero-entry tenant renders the no-entries
// line without the header row.
func TestPrintListTableEmpty(t *testing.T) {
	var buf bytes.Buffer
	printListTable(&buf, &KbListResponse{Entries: nil})
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
	resp := &KbListResponse{
		Entries: []KbEntryPreview{
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

// TestRunListHappyPath drives runList end-to-end through the auth
// + transport stack against an httptest server.
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
		_ = json.NewEncoder(w).Encode(KbListResponse{
			Entries: []KbEntryPreview{
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
		_ = json.NewEncoder(w).Encode(KbListResponse{
			Entries: []KbEntryPreview{
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
	var decoded KbListResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout is not valid JSON: %v\n%s", err, stdout.String())
	}
	if len(decoded.Entries) != 1 || decoded.Entries[0].Slug != "a" {
		t.Errorf("--json decode produced %+v", decoded)
	}
}

// TestRunList401SurfacesAuthExpired — exhausting the refresh budget
// (no refresh_token present) must render as auth_expired with the
// `meho login` hint.
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
