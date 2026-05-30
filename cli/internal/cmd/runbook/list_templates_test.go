// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

func newTemplateSummary(slug string, version int, status api.TemplateSummaryStatus, kind string) api.TemplateSummary {
	tk := kind
	var tkPtr *string
	if kind != "" {
		tkPtr = &tk
	}
	return api.TemplateSummary{
		Slug:       slug,
		Version:    version,
		Title:      "Title for " + slug,
		Status:     status,
		TargetKind: tkPtr,
		EditedAt:   time.Date(2026, 5, 30, 12, 0, 0, 0, time.UTC),
	}
}

// TestRunListTemplatesHappyPath — GET hits the right path, query
// params are honoured, and the human table renders with all five
// columns.
func TestRunListTemplatesHappyPath(t *testing.T) {
	var lastQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		lastQuery = r.URL.RawQuery
		resp := api.RunbookTemplateListResponse{Templates: []api.TemplateSummary{
			newTemplateSummary("vcenter-cert-rotation", 3, api.TemplateSummaryStatusPublished, "vmware-rest"),
			newTemplateSummary("vault-unseal", 1, api.TemplateSummaryStatusDraft, ""),
		}}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(resp)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{
		Status:            "published",
		TargetKind:        "vmware-rest",
		Limit:             50,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runListTemplates: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(lastQuery, "status=published") {
		t.Errorf("expected status=published in query; got %q", lastQuery)
	}
	if !strings.Contains(lastQuery, "target_kind=vmware-rest") {
		t.Errorf("expected target_kind in query; got %q", lastQuery)
	}
	if !strings.Contains(lastQuery, "limit=50") {
		t.Errorf("expected limit=50 in query; got %q", lastQuery)
	}
	out := stdout.String()
	for _, want := range []string{
		"SLUG", "VERSION", "STATUS", "TARGET_KIND", "EDITED_AT",
		"vcenter-cert-rotation", "vault-unseal",
		"published", "draft", "vmware-rest",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected stdout to contain %q; got:\n%s", want, out)
		}
	}
}

// TestRunListTemplatesEmpty — empty list emits a one-liner, not a
// header-only table.
func TestRunListTemplatesEmpty(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(api.RunbookTemplateListResponse{Templates: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListTemplates empty: %v", err)
	}
	if !strings.Contains(stdout.String(), "no runbook templates") {
		t.Errorf("expected empty-list hint; got %q", stdout.String())
	}
}

// TestRunListTemplatesJSON — --json emits the round-tripped envelope.
func TestRunListTemplatesJSON(t *testing.T) {
	expected := api.RunbookTemplateListResponse{Templates: []api.TemplateSummary{
		newTemplateSummary("x", 1, api.TemplateSummaryStatusDraft, ""),
	}}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(expected)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{JSONOut: true, BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runListTemplates --json: %v", err)
	}
	var decoded api.RunbookTemplateListResponse
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(decoded.Templates) != 1 || decoded.Templates[0].Slug != "x" {
		t.Errorf("envelope: %+v", decoded)
	}
}

// TestRunListTemplatesRejectsBadStatus — bad --status fails fast.
func TestRunListTemplatesRejectsBadStatus(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{Status: "pub"})
	if err == nil {
		t.Fatal("expected error for bad --status")
	}
	if !strings.Contains(stderr.String(), "draft, published, deprecated") {
		t.Errorf("expected enum hint; got %q", stderr.String())
	}
}

// TestRunListTemplatesRejectsOutOfRangeLimit — --limit > 500 fails
// fast (mirrors the backend's Query(le=500) cap).
func TestRunListTemplatesRejectsOutOfRangeLimit(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{Limit: 501})
	if err == nil {
		t.Fatal("expected error for oversized --limit")
	}
	if !strings.Contains(stderr.String(), "between 1 and 500") {
		t.Errorf("expected range hint; got %q", stderr.String())
	}
}

// TestRunListTemplates403SurfacesInsufficientRole — operator-role
// JWT lands 403; the CLI must classify as insufficient_role exit 5.
func TestRunListTemplates403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/templates", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: operator required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(stderr.String(), "Insufficient role") {
		t.Errorf("expected role hint; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunListTemplatesNetworkError — when the backend refuses the
// connection, the CLI classifies as unreachable exit 3.
func TestRunListTemplatesNetworkError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	srv.Close() // close immediately so connections refuse
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runListTemplates(cmd, listTemplatesOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatal("expected error on closed server")
	}
	if !strings.Contains(stderr.String(), "unreachable") {
		t.Errorf("expected unreachable classification; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 3 {
		t.Errorf("expected ExitCode 3; got %v", err)
	}
}

// TestListTemplatesParamsOmitsZeroValues — pointer fields stay nil
// when the operator didn't supply the corresponding flag, so the
// backplane's defaults apply.
func TestListTemplatesParamsOmitsZeroValues(t *testing.T) {
	got := listTemplatesParams(listTemplatesOptions{})
	if got.Status != nil {
		t.Errorf("expected nil Status; got %v", *got.Status)
	}
	if got.TargetKind != nil {
		t.Errorf("expected nil TargetKind; got %v", *got.TargetKind)
	}
	if got.Limit != nil {
		t.Errorf("expected nil Limit; got %v", *got.Limit)
	}
}

// TestListTemplatesParamsSetsAllFilters — supplied flags reach the
// typed params shape with the right discriminator.
func TestListTemplatesParamsSetsAllFilters(t *testing.T) {
	got := listTemplatesParams(listTemplatesOptions{
		Status: "draft", TargetKind: "vmware-rest", Limit: 25,
	})
	if got.Status == nil || string(*got.Status) != "draft" {
		t.Errorf("expected Status=draft; got %+v", got.Status)
	}
	if got.TargetKind == nil || *got.TargetKind != "vmware-rest" {
		t.Errorf("expected TargetKind=vmware-rest; got %+v", got.TargetKind)
	}
	if got.Limit == nil || *got.Limit != 25 {
		t.Errorf("expected Limit=25; got %+v", got.Limit)
	}
}
