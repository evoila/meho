// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/startinggrants"
)

func editOpts(url string, grants startinggrants.Flags) editOptions {
	return editOptions{
		Name: "incident-triage", IdentityRef: "agent:incident-triage", identityRefSet: true,
		Grants: grants, JSONOut: true, BackplaneOverride: url,
	}
}

func TestRunEditSendsThePickAndShowsTheGrants(t *testing.T) {
	var seen map[string]any
	var grantQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&seen); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(sampleDefinition())
	})
	mux.HandleFunc("/api/v1/agents/grants", func(w http.ResponseWriter, r *http.Request) {
		grantQuery = r.URL.Query().Get("principal_sub")
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AgentGrantListResponse{Grants: []api.AgentGrantRead{}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runEdit(cmd, editOpts(srv.URL, startinggrants.Flags{Set: "read-everything"})); err != nil {
		t.Fatalf("runEdit: %v; stderr=%s", err, stderr.String())
	}
	pick, ok := seen["starting_grants"].(map[string]any)
	if !ok || pick["set"] != "read-everything" || seen["identity_ref"] != "agent:incident-triage" {
		t.Errorf("PATCH body: %+v", seen)
	}
	if grantQuery != "agent:incident-triage" {
		t.Errorf("grant read principal_sub: %q", grantQuery)
	}
	var out map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
		t.Fatalf("decode %q: %v", stdout.String(), err)
	}
	if grants, ok := out["grants"].([]any); !ok || len(grants) != 0 {
		t.Errorf("json output: %s", stdout.String())
	}
}

func TestRunEditWithoutGrantsFlagSendsNoPick(t *testing.T) {
	var raw map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(sampleDefinition())
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runEdit(cmd, editOpts(srv.URL, startinggrants.Flags{})); err != nil {
		t.Fatalf("runEdit: %v; stderr=%s", err, stderr.String())
	}
	if _, ok := raw["starting_grants"]; ok {
		t.Errorf("starting_grants must be left off the wire: %+v", raw)
	}
	if strings.Contains(stdout.String(), `"grants"`) {
		t.Errorf("no grant read without --grants: %s", stdout.String())
	}
}

func TestRunEditPrintsTheChoicesWhenMEHONeedsAPick(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":"starting_grants_required: pick the agent's starting grants"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	if err := runEdit(cmd, editOpts(srv.URL, startinggrants.Flags{})); err == nil {
		t.Fatal("want an error")
	}
	for _, want := range []string{"usage_error", "Nothing was changed.", "--grants none", "--grants read-everything", "--grants read-targets"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr missing %q; got %q", want, stderr.String())
		}
	}
	if strings.Contains(stderr.String(), "Nothing was created.") {
		t.Errorf("an edit creates nothing; got %q", stderr.String())
	}
}

func TestRunEditShowsAnUnknownTargetError(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":"starting_grants_target_unknown: no target named 'x' in this tenant. Nothing was created."}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runEdit(cmd, editOpts(srv.URL, startinggrants.Flags{Set: "read-targets", Targets: []string{"x"}}))
	if err == nil || !strings.Contains(stderr.String(), "no target named 'x'") {
		t.Fatalf("err=%v stderr=%q", err, stderr.String())
	}
}

func TestRunEditRefusesGrantsWithoutIdentityRef(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	opts := editOpts("http://unused.invalid", startinggrants.Flags{Set: "none"})
	opts.identityRefSet = false
	opts.Enabled, opts.enabledSet = true, true
	err := runEdit(cmd, opts)
	if err == nil || !strings.Contains(stderr.String(), "--grants needs --identity-ref") {
		t.Fatalf("err=%v stderr=%q", err, stderr.String())
	}
}

func TestRunEditRefusesTargetWithoutReadTargets(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runEdit(cmd, editOpts("http://unused.invalid", startinggrants.Flags{Targets: []string{"target-a"}}))
	if err == nil || !strings.Contains(stderr.String(), "--target needs --grants read-targets") {
		t.Fatalf("err=%v stderr=%q", err, stderr.String())
	}
}

func TestEditCommandHasTheGrantFlags(t *testing.T) {
	cmd := newEditCmd()
	for _, name := range []string{"grants", "target"} {
		if cmd.Flags().Lookup(name) == nil {
			t.Errorf("missing --%s", name)
		}
	}
}
