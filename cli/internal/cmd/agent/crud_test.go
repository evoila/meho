// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func sampleEntry() Entry {
	return Entry{
		ID:           "11111111-1111-1111-1111-111111111111",
		TenantID:     "22222222-2222-2222-2222-222222222222",
		Name:         "incident-triage",
		IdentityRef:  "agent:incident-triage",
		ModelTier:    "deep",
		SystemPrompt: "You triage incidents.",
		Toolset:      map[string]any{"allow": []any{"call_operation"}},
		TurnBudget:   25,
		Enabled:      true,
		CreatedBySub: "op-admin",
		CreatedAt:    "2026-05-24T00:00:00Z",
		UpdatedAt:    "2026-05-24T00:00:00Z",
	}
}

// --- list ---

func TestBuildListPath(t *testing.T) {
	if got := buildListPath(listOptions{}); got != "/api/v1/agents" {
		t.Fatalf("empty opts: got %q", got)
	}
	got := buildListPath(listOptions{Limit: 25, Offset: 10})
	for _, want := range []string{"limit=25", "offset=10"} {
		if !strings.Contains(got, want) {
			t.Errorf("buildListPath missing %q in %q", want, got)
		}
	}
}

func TestRunListHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		_ = json.NewEncoder(w).Encode(ListResponse{Agents: []Entry{sampleEntry()}})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{"NAME", "incident-triage", "deep"} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout missing %q in %q", want, stdout.String())
		}
	}
}

func TestPrintListTableEmpty(t *testing.T) {
	var sb strings.Builder
	printListTable(&sb, &ListResponse{})
	if !strings.Contains(sb.String(), "no agent definitions") {
		t.Errorf("empty render missing hint; got %q", sb.String())
	}
}

// --- show ---

func TestBuildShowPathEscapes(t *testing.T) {
	if got := buildShowPath("vm.inventory-bot"); got != "/api/v1/agents/vm.inventory-bot" {
		t.Fatalf("buildShowPath: got %q", got)
	}
}

func TestRunShowHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(sampleEntry())
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runShow(cmd, showOptions{Name: "incident-triage", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "incident-triage") {
		t.Errorf("stdout missing name; got %q", stdout.String())
	}
}

func TestRunShow404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/nope", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"agent_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runShow(cmd, showOptions{Name: "nope", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "agent_not_found") {
		t.Errorf("stderr missing agent_not_found; got %q", stderr.String())
	}
}

// --- create ---

func TestRunCreateHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method: got %s; want POST", r.Method)
		}
		var body createRequest
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body.Name != "incident-triage" || body.ModelTier != "deep" || body.TurnBudget != 25 {
			t.Errorf("unexpected request body: %+v", body)
		}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(sampleEntry())
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runCreate(cmd, createOptions{
		Name:              "incident-triage",
		IdentityRef:       "agent:incident-triage",
		ModelTier:         "deep",
		SystemPrompt:      "You triage incidents.",
		TurnBudget:        25,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "created agent definition") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

func TestRunCreateRejectsBadModelTier(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", IdentityRef: "a", ModelTier: "ultra", SystemPrompt: "p", TurnBudget: 5,
	})
	if err == nil {
		t.Fatalf("expected error for bad model tier")
	}
	if !strings.Contains(stderr.String(), "standard, fast, deep") {
		t.Errorf("stderr missing tier hint; got %q", stderr.String())
	}
}

func TestRunCreateRejectsOutOfRangeBudget(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", IdentityRef: "a", ModelTier: "deep", SystemPrompt: "p", TurnBudget: 5000,
	})
	if err == nil {
		t.Fatalf("expected error for out-of-range budget")
	}
	if !strings.Contains(stderr.String(), "between 1 and 1000") {
		t.Errorf("stderr missing budget hint; got %q", stderr.String())
	}
}

func TestRunCreate409SurfacesConflict(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		fmt.Fprint(w, `{"detail":"agent_already_exists"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "dup", IdentityRef: "a", ModelTier: "deep", SystemPrompt: "p", TurnBudget: 5,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 409")
	}
	if !strings.Contains(stderr.String(), "agent_already_exists") {
		t.Errorf("stderr missing conflict detail; got %q", stderr.String())
	}
}

func TestRunCreate403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		fmt.Fprint(w, `{"detail":"Insufficient role: tenant_admin required"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", IdentityRef: "a", ModelTier: "deep", SystemPrompt: "p", TurnBudget: 5,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 403")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("stderr missing insufficient_role; got %q", stderr.String())
	}
}

// --- edit ---

func TestBuildEditBodyOnlyChangedFields(t *testing.T) {
	cmd, _, _ := newTestCmd(t)
	body, err := buildEditBody(cmd, editOptions{
		TurnBudget: 50, turnBudgetSet: true,
		disabledSet: true,
	})
	if err != nil {
		t.Fatalf("buildEditBody: %v", err)
	}
	if len(body) != 2 {
		t.Fatalf("expected 2 fields; got %+v", body)
	}
	if body["turn_budget"] != 50 {
		t.Errorf("turn_budget: got %v", body["turn_budget"])
	}
	if body["enabled"] != false {
		t.Errorf("enabled: got %v", body["enabled"])
	}
}

func TestBuildEditBodyRejectsBadTier(t *testing.T) {
	cmd, _, _ := newTestCmd(t)
	_, err := buildEditBody(cmd, editOptions{ModelTier: "ultra", modelTierSet: true})
	if err == nil {
		t.Fatalf("expected error for bad model tier")
	}
}

func TestRunEditNoFieldsIsError(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runEdit(cmd, editOptions{Name: "x"})
	if err == nil {
		t.Fatalf("expected error when no field flags set")
	}
	if !strings.Contains(stderr.String(), "at least one field") {
		t.Errorf("stderr missing no-op hint; got %q", stderr.String())
	}
}

func TestRunEditEnabledDisabledConflict(t *testing.T) {
	cmd, _, stderr := newTestCmd(t)
	err := runEdit(cmd, editOptions{Name: "x", enabledSet: true, disabledSet: true})
	if err == nil {
		t.Fatalf("expected error when both --enabled and --disabled set")
	}
	if !strings.Contains(stderr.String(), "at most one") {
		t.Errorf("stderr missing conflict hint; got %q", stderr.String())
	}
}

func TestRunEditHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPatch {
			t.Errorf("method: got %s; want PATCH", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		if !strings.Contains(string(raw), "turn_budget") {
			t.Errorf("PATCH body missing turn_budget: %s", raw)
		}
		entry := sampleEntry()
		entry.TurnBudget = 50
		_ = json.NewEncoder(w).Encode(entry)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runEdit(cmd, editOptions{
		Name: "incident-triage", TurnBudget: 50, turnBudgetSet: true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runEdit: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "updated agent definition") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

// --- delete ---

func TestRunDeleteConfirmHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/incident-triage", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			t.Errorf("method: got %s; want DELETE", r.Method)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newTestCmd(t)
	err := runDelete(cmd, deleteOptions{
		Name: "incident-triage", Confirm: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runDelete: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "deleted agent definition") {
		t.Errorf("stdout missing confirmation; got %q", stdout.String())
	}
}

func TestRunDeleteDeclinedExitsZero(t *testing.T) {
	cmd, stdout, _ := newTestCmd(t)
	cmd.SetIn(strings.NewReader("n\n"))
	err := runDelete(cmd, deleteOptions{Name: "incident-triage"})
	if err != nil {
		t.Fatalf("declined delete should exit 0; got %v", err)
	}
	if !strings.Contains(stdout.String(), "declined") {
		t.Errorf("stdout missing declined line; got %q", stdout.String())
	}
}

func TestRunDelete404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/nope", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"agent_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newTestCmd(t)
	err := runDelete(cmd, deleteOptions{Name: "nope", Confirm: true, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "agent_not_found") {
		t.Errorf("stderr missing agent_not_found; got %q", stderr.String())
	}
}
