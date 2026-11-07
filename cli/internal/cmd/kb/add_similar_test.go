// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const similarAnswer = `{
  "slug": "new-slug", "written": false, "reason": "%s", "would_write": %s,
  "slug_exists": false, "min_score": 0.85, "next_step": "n",
  "similar": [{
    "slug": "old-entry", "entry_id": "00000000-0000-0000-0000-000000000001",
    "revision_id": "00000000-0000-0000-0000-000000000002",
    "summary": "Power the VM off first", "product": "vcenter", "version": "8.0",
    "status": "confirmed", "snippet": "old text", "score": 0.93,
    "match": "same_product_version", "may_change": false
  }]
}`

// similarServer answers POST /api/v1/kb with status and body, and keeps
// the request body it got.
func similarServer(t *testing.T, status int, body string, got *api.KbEntryCreate) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, got)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv
}

func TestRunAdd409ListsSimilarEntriesAndFails(t *testing.T) {
	var got api.KbEntryCreate
	answer := strings.Replace(strings.Replace(similarAnswer, "%s", "similar_entries", 1), "%s", "false", 1)
	srv := similarServer(t, http.StatusConflict, answer, &got)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{Slug: "new-slug", BodyArg: "b", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected an error when similar entries exist")
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 1 {
		t.Errorf("expected exit code 1; got %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"not written", "old-entry", "same product and version",
		"you may not change it", "Power the VM off first",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in output; got %q", want, out)
		}
	}
	if !strings.Contains(stderr.String(), "similar_entries") ||
		!strings.Contains(stderr.String(), "--create-anyway") {
		t.Errorf("expected similar_entries error with the --create-anyway hint; got %q", stderr.String())
	}
	if got.CreateAnyway != nil || got.DryRun != nil {
		t.Errorf("expected no create_anyway / dry_run on the wire; got %+v", got)
	}
}

func TestRunAddDryRunListsSimilarEntriesAndSucceeds(t *testing.T) {
	var got api.KbEntryCreate
	answer := strings.Replace(strings.Replace(similarAnswer, "%s", "dry_run", 1), "%s", "false", 1)
	srv := similarServer(t, http.StatusOK, answer, &got)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{
		Slug: "new-slug", BodyArg: "b", DryRun: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAdd: %v; stderr=%s", err, stderr.String())
	}
	if got.DryRun == nil || !*got.DryRun {
		t.Errorf("expected dry_run=true on the wire; got %+v", got)
	}
	if !strings.Contains(stdout.String(), "would not go through") ||
		!strings.Contains(stdout.String(), "old-entry") {
		t.Errorf("expected the dry-run summary; got %q", stdout.String())
	}
}

func TestRunAddDryRunJSONPrintsTheAnswer(t *testing.T) {
	var got api.KbEntryCreate
	answer := `{"slug": "new-slug", "written": false, "reason": "dry_run", "would_write": true,
	  "slug_exists": false, "min_score": 0.85, "next_step": "n", "similar": []}`
	srv := similarServer(t, http.StatusOK, answer, &got)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{
		Slug: "new-slug", BodyArg: "b", DryRun: true, JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAdd: %v", err)
	}
	var check api.KbSimilarCheck
	if err := json.Unmarshal(stdout.Bytes(), &check); err != nil {
		t.Fatalf("decode stdout: %v; %q", err, stdout.String())
	}
	if !check.WouldWrite || check.Reason != api.KbSimilarCheckReasonDryRun {
		t.Errorf("expected would_write dry run; got %+v", check)
	}
}

func TestRunAddCreateAnywaySendsTheFlag(t *testing.T) {
	var got api.KbEntryCreate
	entry, _ := json.Marshal(newKbEntry(t, "new-slug", "b"))
	srv := similarServer(t, http.StatusCreated, string(entry), &got)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{
		Slug: "new-slug", BodyArg: "b", CreateAnyway: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAdd: %v; stderr=%s", err, stderr.String())
	}
	if got.CreateAnyway == nil || !*got.CreateAnyway {
		t.Errorf("expected create_anyway=true on the wire; got %+v", got)
	}
	if !strings.Contains(stdout.String(), "created kb entry") {
		t.Errorf("expected the created line; got %q", stdout.String())
	}
}

func TestRunAdd409WithoutJSONIsUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/kb", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte("not-json"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runAdd(cmd, addOptions{Slug: "x", BodyArg: "y", BackplaneOverride: srv.URL})
	if err == nil || !strings.Contains(stderr.String(), "unexpected_response") {
		t.Fatalf("expected unexpected_response; got err=%v stderr=%q", err, stderr.String())
	}
}
