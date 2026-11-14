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
	"time"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/startinggrants"
)

func createOpts(url string, grants startinggrants.Flags) createOptions {
	return createOptions{
		Name: "incident-triage", IdentityRef: "agent:incident-triage", ModelTier: "deep",
		SystemPrompt: "You triage incidents.", TurnBudget: 25,
		Grants: grants, JSONOut: true, BackplaneOverride: url,
	}
}

func TestRunCreateSendsThePickAndShowsTheGrants(t *testing.T) {
	var seen api.AgentDefinitionCreate
	var grantQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents", func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&seen); err != nil {
			t.Fatalf("decode: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(sampleDefinition())
	})
	mux.HandleFunc("/api/v1/agents/grants", func(w http.ResponseWriter, r *http.Request) {
		grantQuery = r.URL.Query().Get("principal_sub")
		at := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
		scope := "33333333-3333-3333-3333-333333333333"
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.AgentGrantListResponse{Grants: []api.AgentGrantRead{{
			Id: uuid.MustParse("44444444-4444-4444-4444-444444444444"), PrincipalSub: "agent:incident-triage",
			OpPattern: "*", TargetScope: &scope, Verdict: "auto-execute", CreatedAt: at, UpdatedAt: at,
		}}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runCreate(cmd, createOpts(srv.URL, startinggrants.Flags{Set: "read-targets", Targets: []string{"target-a"}}))
	if err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if seen.StartingGrants == nil || seen.StartingGrants.Set != api.StartingGrantSetReadTargets ||
		seen.StartingGrants.Targets == nil || (*seen.StartingGrants.Targets)[0] != "target-a" {
		t.Errorf("starting_grants on the wire: %+v", seen.StartingGrants)
	}
	if grantQuery != "agent:incident-triage" {
		t.Errorf("grant read principal_sub: %q", grantQuery)
	}
	var out struct {
		Name   string               `json:"name"`
		Grants []api.AgentGrantRead `json:"grants"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
		t.Fatalf("decode %q: %v", stdout.String(), err)
	}
	if out.Name != "incident-triage" || len(out.Grants) != 1 {
		t.Errorf("json output: %s", stdout.String())
	}
}

func TestRunCreatePrintsTheChoicesWhenMEHONeedsAPick(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		fmt.Fprint(w, `{"detail":"starting_grants_required: pick the agent's starting grants"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runCreate(cmd, createOpts(srv.URL, startinggrants.Flags{}))
	if err == nil {
		t.Fatal("want an error")
	}
	for _, want := range []string{"usage_error", "--grants none", "--grants read-everything", "--grants read-targets"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr missing %q; got %q", want, stderr.String())
		}
	}
}

func TestRunCreateRefusesTargetWithoutReadTargets(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runCreate(cmd, createOpts("http://unused.invalid", startinggrants.Flags{Targets: []string{"target-a"}}))
	if err == nil || !strings.Contains(stderr.String(), "--target needs --grants read-targets") {
		t.Fatalf("err=%v stderr=%q", err, stderr.String())
	}
}

func TestCreateCommandHasTheGrantFlags(t *testing.T) {
	cmd := newCreateCmd()
	for _, name := range []string{"grants", "target"} {
		if cmd.Flags().Lookup(name) == nil {
			t.Errorf("missing --%s", name)
		}
	}
}
