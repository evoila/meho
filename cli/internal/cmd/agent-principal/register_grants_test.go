// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agentprincipal

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/startinggrants"
)

func grantFor(sub string) api.AgentGrantRead {
	at := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	scope := "*"
	return api.AgentGrantRead{
		Id:           uuid.MustParse("44444444-4444-4444-4444-444444444444"),
		TenantId:     uuid.MustParse(stubTenantID),
		PrincipalSub: sub,
		OpPattern:    "*",
		TargetScope:  &scope,
		Verdict:      "auto-execute",
		CreatedBySub: "creator-sub",
		CreatedAt:    at,
		UpdatedAt:    at,
	}
}

func TestRegisterSendsTheReadTargetsPick(t *testing.T) {
	var seen api.AgentPrincipalCreate
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, r *http.Request) {
			seen = readJSONBody[api.AgentPrincipalCreate](t, r)
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, seen.Name, false))
		},
		"GET /api/v1/agents/grants": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, api.AgentGrantListResponse{Grants: []api.AgentGrantRead{}})
		},
	})
	defer srv.Close()

	_, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name:              "bot",
			Grants:            startinggrants.Flags{Set: "read-targets", Targets: []string{"target-a", "target-b"}},
			JSONOut:           true,
			BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("runRegister: %v", err)
	}
	if seen.StartingGrants == nil || seen.StartingGrants.Set != api.StartingGrantSetReadTargets ||
		seen.StartingGrants.Targets == nil || strings.Join(*seen.StartingGrants.Targets, ",") != "target-a,target-b" {
		t.Errorf("starting_grants on the wire: %+v", seen.StartingGrants)
	}
}

func TestRegisterWithoutGrantsFlagSendsNoPick(t *testing.T) {
	var raw map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, r *http.Request) {
			raw = readJSONBody[map[string]any](t, r)
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, "bot", false))
		},
	})
	defer srv.Close()

	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{Name: "bot", JSONOut: true, BackplaneOverride: srv.URL})
	})
	if err != nil {
		t.Fatalf("runRegister: %v", err)
	}
	if _, ok := raw["starting_grants"]; ok {
		t.Errorf("starting_grants must be left off the wire: %+v", raw)
	}
	var out map[string]any
	_ = json.Unmarshal(stdout.Bytes(), &out)
	if _, ok := out["grants"]; ok {
		t.Errorf("no grants key without --grants: %s", stdout.String())
	}
}

func TestRegisterJSONIncludesTheWrittenGrants(t *testing.T) {
	var grantQuery string
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, "bot", false))
		},
		"GET /api/v1/agents/grants": func(w http.ResponseWriter, r *http.Request) {
			grantQuery = r.URL.Query().Get("principal_sub")
			writeJSON(t, w, http.StatusOK, api.AgentGrantListResponse{Grants: []api.AgentGrantRead{grantFor("agent:bot")}})
		},
	})
	defer srv.Close()

	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "bot", Grants: startinggrants.Flags{Set: "read-everything"},
			JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("runRegister: %v", err)
	}
	if grantQuery != "agent:bot" {
		t.Errorf("grant read principal_sub: %q", grantQuery)
	}
	var out struct {
		Name   string               `json:"name"`
		Grants []api.AgentGrantRead `json:"grants"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &out); err != nil {
		t.Fatalf("decode %q: %v", stdout.String(), err)
	}
	if out.Name != "bot" || len(out.Grants) != 1 || out.Grants[0].OpPattern != "*" {
		t.Errorf("json output: %s", stdout.String())
	}
}

func TestRegisterHumanOutputShowsTheGrants(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, "bot", false))
		},
		"GET /api/v1/agents/grants": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusOK, api.AgentGrantListResponse{Grants: []api.AgentGrantRead{grantFor("agent:bot")}})
		},
	})
	defer srv.Close()

	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "bot", Grants: startinggrants.Flags{Set: "read-everything"}, BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("runRegister: %v", err)
	}
	if !strings.Contains(stdout.String(), "grants of agent:bot:") || !strings.Contains(stdout.String(), "target=every target") {
		t.Errorf("stdout:\n%s", stdout.String())
	}
}

func TestRegisterJSONMarksAFailedGrantReadAsUnknown(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusCreated, newPrincipalRead(t, "bot", false))
		},
		"GET /api/v1/agents/grants": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusInternalServerError, map[string]string{"detail": "boom"})
		},
	})
	defer srv.Close()

	stdout, _, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "bot", Grants: startinggrants.Flags{Set: "none"},
			JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	if err != nil {
		t.Fatalf("the agent was created, so the command succeeds: %v", err)
	}
	if !strings.Contains(stdout.String(), `"grants": null`) || !strings.Contains(stdout.String(), `"grants_error"`) {
		t.Errorf("stdout: %s", stdout.String())
	}
}

func TestRegisterPrintsTheChoicesWhenMEHONeedsAPick(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusUnprocessableEntity,
				map[string]string{"detail": "starting_grants_required: pick the agent's starting grants"})
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{Name: "bot", JSONOut: true, BackplaneOverride: srv.URL})
	})
	assertRenderedErrorCode(t, stderr, err, "usage_error", "--grants read-targets --target")
}

func TestRegisterShowsAnUnknownTargetError(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/agent-principals": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, http.StatusUnprocessableEntity,
				map[string]string{"detail": "starting_grants_target_unknown: no target named 'x' in this tenant. Nothing was created."})
		},
	})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "bot", Grants: startinggrants.Flags{Set: "read-targets", Targets: []string{"x"}},
			JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	assertRenderedErrorCode(t, stderr, err, "usage_error", "no target named 'x'")
}

func TestRegisterRefusesTargetWithoutReadTargets(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{})
	defer srv.Close()

	_, stderr, err := runVerbWithMock(t, srv, func(cmd *cobra.Command) error {
		return runRegister(cmd, registerOptions{
			Name: "bot", Grants: startinggrants.Flags{Set: "read-everything", Targets: []string{"x"}},
			JSONOut: true, BackplaneOverride: srv.URL,
		})
	})
	assertRenderedErrorCode(t, stderr, err, "usage_error", "only used with --grants read-targets")
}

func TestRegisterCommandHasTheGrantFlags(t *testing.T) {
	cmd := newRegisterCmd()
	for _, name := range []string{"grants", "target"} {
		if cmd.Flags().Lookup(name) == nil {
			t.Errorf("missing --%s", name)
		}
	}
}
