// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// seedXDGAndToken seeds a per-test config dir + token store the way
// the sibling test files do (mirrors `cli/internal/cmd/memory/memory_test.go`).
// `MEHO_KEYRING_DISABLE=1` forces the file-backed token store path so
// the test never touches the OS keyring.
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

// newRunCmd builds a fresh cobra.Command with stdout/stderr buffers.
// The runXxx helpers consume cmd.OutOrStdout / cmd.ErrOrStderr;
// tests inspect the buffers afterwards.
func newRunCmd(t *testing.T) (*cobra.Command, *bytes.Buffer, *bytes.Buffer) {
	t.Helper()
	cmd := &cobra.Command{}
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	cmd.SetContext(ctx)
	return cmd, &stdout, &stderr
}

// stubTriggerID is the fixed trigger UUID used in every test
// fixture. Reading the literal "11111111-..." in a failing
// assertion makes the failure easier to chase than a random
// uuid.New() that changes per test run.
const (
	stubTriggerID   = "11111111-1111-1111-1111-111111111111"
	stubTenantID    = "22222222-2222-2222-2222-222222222222"
	stubAgentDefID  = "33333333-3333-3333-3333-333333333333"
	stubOtherTenant = "44444444-4444-4444-4444-444444444444"
)

func parseStubUUID(t *testing.T, s string) openapi_types.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("uuid.Parse(%q): %v", s, err)
	}
	return id
}

// fakeTrigger builds a minimal `api.ScheduledTriggerRead` fixture
// keyed off the package's stub UUIDs. Callers override fields per
// test (e.g. setting CronExpr for a cron-trigger row).
func fakeTrigger(t *testing.T, kind string) api.ScheduledTriggerRead {
	t.Helper()
	now := time.Date(2026, 5, 28, 12, 0, 0, 0, time.UTC)
	return api.ScheduledTriggerRead{
		Id:                parseStubUUID(t, stubTriggerID),
		TenantId:          parseStubUUID(t, stubTenantID),
		AgentDefinitionId: parseStubUUID(t, stubAgentDefID),
		Kind:              api.ScheduledTriggerKind(kind),
		Status:            api.ScheduledTriggerStatus("active"),
		InFlightPolicy:    api.ScheduledTriggerInFlightPolicy("fail_into_audit"),
		Timezone:          "UTC",
		IdentitySub:       "__scheduler__",
		CreatedBySub:      "alice@example.com",
		CreatedAt:         now,
		UpdatedAt:         now,
	}
}

// ---------------------------------------------------------------
// Subcommand wiring
// ---------------------------------------------------------------

func TestNewRootCmd_Subcommands(t *testing.T) {
	cmd := NewRootCmd()
	if cmd.Use != "scheduler" {
		t.Fatalf("expected Use=scheduler, got %q", cmd.Use)
	}
	want := map[string]bool{"list": true, "create": true, "cancel <trigger_id>": true}
	for _, sub := range cmd.Commands() {
		if !want[sub.Use] {
			t.Errorf("unexpected subcommand %q", sub.Use)
		}
		delete(want, sub.Use)
	}
	if len(want) != 0 {
		t.Errorf("missing subcommands: %v", want)
	}
}

// ---------------------------------------------------------------
// Enum guards
// ---------------------------------------------------------------

func TestValidKinds(t *testing.T) {
	for _, k := range []string{"cron", "one_off", "event"} {
		if !validKinds[k] {
			t.Errorf("expected %q to be a valid kind", k)
		}
	}
	if validKinds["bogus"] {
		t.Errorf("expected 'bogus' to not be a valid kind")
	}
}

func TestValidStatuses(t *testing.T) {
	for _, s := range []string{"active", "paused", "cancelled", "fired"} {
		if !validStatuses[s] {
			t.Errorf("expected %q to be a valid status", s)
		}
	}
}

func TestValidInFlightPolicies(t *testing.T) {
	for _, p := range []string{"fail_into_audit", "resume"} {
		if !validInFlightPolicies[p] {
			t.Errorf("expected %q to be a valid in-flight policy", p)
		}
	}
}

// ---------------------------------------------------------------
// JSON-object flag loader (review M3 / M4 on PR #1128 — unchanged
// by the G0.12-T13 typed-client migration).
// ---------------------------------------------------------------

// TestLoadJSONObjectFlag_RejectsJSONNull covers review M3 on PR #1128.
func TestLoadJSONObjectFlag_RejectsJSONNull(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(strings.NewReader(""))
	cases := []struct {
		name string
		raw  string
	}{
		{name: "literal_null", raw: "null"},
		{name: "literal_null_with_whitespace", raw: "  null  "},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			out, err := loadJSONObjectFlag(cmd, tc.raw, "event-filter")
			if err == nil {
				t.Fatalf("expected error for JSON null, got out=%v err=nil", out)
			}
			if !strings.Contains(err.Error(), "got null") {
				t.Errorf("expected error to mention 'got null', got: %v", err)
			}
		})
	}
}

func TestLoadJSONObjectFlag_RejectsJSONNullViaStdin(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(strings.NewReader("null\n"))
	out, err := loadJSONObjectFlag(cmd, "@-", "inputs")
	if err == nil {
		t.Fatalf("expected error for JSON null from stdin, got out=%v err=nil", out)
	}
	if !strings.Contains(err.Error(), "got null") {
		t.Errorf("expected error to mention 'got null', got: %v", err)
	}
}

// TestLoadJSONObjectFlag_RejectsOverCapFile covers review M4 on PR #1128.
func TestLoadJSONObjectFlag_RejectsOverCapFile(t *testing.T) {
	original := readJSONFile
	t.Cleanup(func() { readJSONFile = original })

	readJSONFile = func(_ string) ([]byte, error) {
		return nil, &capExceededError{}
	}

	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewReader(nil))
	out, err := loadJSONObjectFlag(cmd, "@/tmp/huge.json", "event-filter")
	if err == nil {
		t.Fatalf("expected over-cap file to surface an error, got out=%v err=nil", out)
	}
}

// capExceededError is a stand-in for the cap-exceeded error
// readJSONFile would normally return; used in TestLoadJSONObjectFlag_
// RejectsOverCapFile to confirm the wrapping path lights up.
type capExceededError struct{}

func (capExceededError) Error() string {
	return "file \"/tmp/huge.json\" exceeds 262144-byte cap"
}

// ---------------------------------------------------------------
// listQueryParams — wire-shape pin
// ---------------------------------------------------------------

func TestListQueryParamsOmitsEmptyFilters(t *testing.T) {
	got := listQueryParams(listOptions{}, nil)
	if got.Kind != nil || got.Status != nil || got.TenantFilter != nil ||
		got.Limit != nil || got.Offset != nil {
		t.Errorf("expected all filters nil; got %+v", got)
	}
}

func TestListQueryParamsForwardsFilters(t *testing.T) {
	tenantID := parseStubUUID(t, stubOtherTenant)
	got := listQueryParams(listOptions{
		Kind:   "cron",
		Status: "active",
		Limit:  25,
		Offset: 50,
	}, &tenantID)
	if got.Kind == nil || string(*got.Kind) != "cron" {
		t.Errorf("expected kind=cron forwarded; got %+v", got.Kind)
	}
	if got.Status == nil || string(*got.Status) != "active" {
		t.Errorf("expected status=active forwarded; got %+v", got.Status)
	}
	if got.TenantFilter == nil || got.TenantFilter.String() != stubOtherTenant {
		t.Errorf("expected tenant_filter forwarded; got %+v", got.TenantFilter)
	}
	if got.Limit == nil || *got.Limit != 25 {
		t.Errorf("expected limit=25; got %+v", got.Limit)
	}
	if got.Offset == nil || *got.Offset != 50 {
		t.Errorf("expected offset=50; got %+v", got.Offset)
	}
}

// ---------------------------------------------------------------
// buildCreateBody — wire-shape pin
// ---------------------------------------------------------------

func TestBuildCreateBodyCron(t *testing.T) {
	agentID := parseStubUUID(t, stubAgentDefID)
	body := buildCreateBody(
		createOptions{
			Kind:           "cron",
			CronExpr:       "* * * * *",
			Timezone:       "UTC",
			InFlightPolicy: "resume",
			IdentitySub:    "alice",
		},
		agentID, nil, nil, nil, nil,
	)
	if body.Kind != api.ScheduledTriggerKind("cron") {
		t.Errorf("kind: got %q want cron", body.Kind)
	}
	if body.AgentDefinitionId != agentID {
		t.Errorf("agent_definition_id not forwarded; got %v", body.AgentDefinitionId)
	}
	if body.CronExpr == nil || *body.CronExpr != "* * * * *" {
		t.Errorf("cron_expr not forwarded; got %+v", body.CronExpr)
	}
	if body.Timezone == nil || *body.Timezone != "UTC" {
		t.Errorf("timezone not forwarded; got %+v", body.Timezone)
	}
	if body.InFlightPolicy == nil || string(*body.InFlightPolicy) != "resume" {
		t.Errorf("in_flight_policy not forwarded; got %+v", body.InFlightPolicy)
	}
	if body.IdentitySub == nil || *body.IdentitySub != "alice" {
		t.Errorf("identity_sub not forwarded; got %+v", body.IdentitySub)
	}
	if body.FireAt != nil || body.EventFilter != nil || body.Inputs != nil || body.TenantId != nil {
		t.Errorf("non-cron fields should stay nil; got %+v", body)
	}
}

func TestBuildCreateBodyOneOff(t *testing.T) {
	agentID := parseStubUUID(t, stubAgentDefID)
	when := time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC)
	body := buildCreateBody(
		createOptions{Kind: "one_off"},
		agentID, nil, &when, nil, nil,
	)
	if body.FireAt == nil || !body.FireAt.Equal(when) {
		t.Errorf("fire_at not forwarded; got %+v", body.FireAt)
	}
	if body.CronExpr != nil || body.EventFilter != nil {
		t.Errorf("non-one_off fields should stay nil; got %+v", body)
	}
}

func TestBuildCreateBodyEvent(t *testing.T) {
	agentID := parseStubUUID(t, stubAgentDefID)
	filter := map[string]any{"kind": "agent_run.finished"}
	inputs := map[string]any{"target": "rke2"}
	tenantID := parseStubUUID(t, stubOtherTenant)
	body := buildCreateBody(
		createOptions{Kind: "event"},
		agentID, &tenantID, nil, filter, inputs,
	)
	if body.EventFilter == nil {
		t.Fatalf("event_filter not forwarded; got nil")
	}
	if (*body.EventFilter)["kind"] != "agent_run.finished" {
		t.Errorf("event_filter content wrong: got %+v", *body.EventFilter)
	}
	if body.Inputs == nil || (*body.Inputs)["target"] != "rke2" {
		t.Errorf("inputs not forwarded; got %+v", body.Inputs)
	}
	if body.TenantId == nil || body.TenantId.String() != stubOtherTenant {
		t.Errorf("tenant_id not forwarded; got %+v", body.TenantId)
	}
}

// ---------------------------------------------------------------
// list verb — end-to-end via httptest.Server
// ---------------------------------------------------------------

func TestRunListHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.ScheduledTriggerListResponse{
			Triggers: []api.ScheduledTriggerRead{fakeTrigger(t, "cron")},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), stubTriggerID) {
		t.Errorf("expected trigger id in stdout; got %q", stdout.String())
	}
}

func TestRunListJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.ScheduledTriggerListResponse{
			Triggers: []api.ScheduledTriggerRead{fakeTrigger(t, "cron")},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList --json: %v; stderr=%s", err, stderr.String())
	}
	var resp api.ScheduledTriggerListResponse
	if err := json.Unmarshal(stdout.Bytes(), &resp); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if len(resp.Triggers) != 1 {
		t.Errorf("expected 1 trigger in JSON; got %d", len(resp.Triggers))
	}
}

func TestRunListEmptyResponse(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.ScheduledTriggerListResponse{Triggers: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no scheduled triggers") {
		t.Errorf("expected empty-list message; got %q", stdout.String())
	}
}

func TestRunListForwardsFilters(t *testing.T) {
	var capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, r *http.Request) {
		capturedQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.ScheduledTriggerListResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{
		Kind:              "cron",
		Status:            "active",
		Tenant:            stubOtherTenant,
		Limit:             25,
		Offset:            50,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{
		"kind=cron",
		"status=active",
		"tenant_filter=" + stubOtherTenant,
		"limit=25",
		"offset=50",
	} {
		if !strings.Contains(capturedQuery, want) {
			t.Errorf("expected query %q to contain %q", capturedQuery, want)
		}
	}
}

func TestRunListInvalidKindFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Kind: "bogus"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--kind must be one of") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunListInvalidTenantUUIDFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Tenant: "not-a-uuid"}); err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "--tenant is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

// TestRunList200WithoutPayloadSurfacesUnexpected pins the JSON200
// nil-guard. A 200 without an `application/json` Content-Type
// leaves `resp.JSON200` nil; without the guard the verb would
// print "no scheduled triggers in this tenant" as if the tenant
// genuinely had zero — actively misleading.
func TestRunList200WithoutPayloadSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"triggers":[]}`)) // no Content-Type → JSON200 nil
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected nil-guard error on missing payload")
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a scheduler list payload") {
		t.Errorf("expected nil-guard message; got %q", stderr.String())
	}
}

func TestRunList403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, _ *http.Request) {
		// No Content-Type — the parser exits cleanly with raw Body
		// bytes and StatusCode=403, the verb routes through
		// renderHTTPStatus → InsufficientRole.
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"tenant_filter_requires_tenant_admin"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runList(cmd, listOptions{Tenant: stubOtherTenant, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected 403 to surface as error")
	}
	if !strings.Contains(stderr.String(), "tenant_filter_requires_tenant_admin") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// create verb
// ---------------------------------------------------------------

func TestRunCreateCronHappyPath(t *testing.T) {
	var capturedBody api.ScheduledTriggerCreate
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		if err := json.NewDecoder(r.Body).Decode(&capturedBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		trigger := fakeTrigger(t, "cron")
		cronExpr := "* * * * *"
		trigger.CronExpr = &cronExpr
		_ = json.NewEncoder(w).Encode(trigger)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runCreate(cmd, createOptions{
		Kind:              "cron",
		AgentDefinition:   stubAgentDefID,
		CronExpr:          "* * * * *",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if string(capturedBody.Kind) != "cron" {
		t.Errorf("expected wire kind=cron; got %q", capturedBody.Kind)
	}
	if capturedBody.CronExpr == nil || *capturedBody.CronExpr != "* * * * *" {
		t.Errorf("expected cron_expr on wire; got %+v", capturedBody.CronExpr)
	}
	if !strings.Contains(stdout.String(), "created cron trigger") {
		t.Errorf("expected created-prose in stdout; got %q", stdout.String())
	}
}

func TestRunCreateRejectsUnknownKind(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runCreate(cmd, createOptions{Kind: "bogus"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--kind must be one of") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsCronWithoutExpr(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Kind: "cron", AgentDefinition: stubAgentDefID,
	})
	if err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--kind=cron requires --cron-expr") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsInvalidAgentDefUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Kind:            "cron",
		AgentDefinition: "not-a-uuid",
		CronExpr:        "* * * * *",
	})
	if err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "--agent-definition is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsInvalidFireAt(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Kind:            "one_off",
		AgentDefinition: stubAgentDefID,
		FireAt:          "not-a-timestamp",
	})
	if err == nil {
		t.Fatalf("expected fire-at validation error")
	}
	if !strings.Contains(stderr.String(), "--fire-at must be RFC 3339") {
		t.Errorf("expected fire-at message; got %q", stderr.String())
	}
}

// TestRunCreate201WithoutPayloadSurfacesUnexpected pins the JSON201
// nil-guard.
func TestRunCreate201WithoutPayloadSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{}`)) // no Content-Type → JSON201 nil
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Kind: "cron", AgentDefinition: stubAgentDefID, CronExpr: "* * * * *",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected nil-guard error on missing payload")
	}
	if !strings.Contains(stderr.String(), "HTTP 201 without a created-trigger payload") {
		t.Errorf("expected nil-guard message; got %q", stderr.String())
	}
}

func TestRunCreate422SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers", func(w http.ResponseWriter, _ *http.Request) {
		// Deliberately no Content-Type header — the backend's
		// HTTPException(detail="string") path lands without the
		// `application/json` content type, so the oapi-codegen parser
		// leaves `JSON422` nil but still populates `Body` with the
		// raw bytes. The verb then routes through `renderHTTPStatus`
		// which extracts the detail string via `decodeDetailString`.
		// Mirrors the approach the approvals sibling adopted on PR
		// #1276.
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"agent_definition_not_found"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Kind: "cron", AgentDefinition: stubAgentDefID, CronExpr: "* * * * *",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 422 error")
	}
	if !strings.Contains(stderr.String(), "agent_definition_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// cancel verb
// ---------------------------------------------------------------

func TestRunCancelHappyPath(t *testing.T) {
	var capturedPath, capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers/"+stubTriggerID,
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete {
				t.Errorf("expected DELETE; got %s", r.Method)
			}
			capturedPath = r.URL.Path
			capturedQuery = r.URL.RawQuery
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runCancel(cmd, cancelOptions{
		TriggerID:         stubTriggerID,
		Tenant:            stubOtherTenant,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCancel: %v; stderr=%s", err, stderr.String())
	}
	if capturedPath != "/api/v1/scheduler/triggers/"+stubTriggerID {
		t.Errorf("unexpected path: %q", capturedPath)
	}
	if !strings.Contains(capturedQuery, "tenant_filter="+stubOtherTenant) {
		t.Errorf("expected tenant_filter; got %q", capturedQuery)
	}
	if !strings.Contains(stdout.String(), "cancelled trigger "+stubTriggerID) {
		t.Errorf("expected cancelled-prose; got %q", stdout.String())
	}
}

func TestRunCancelJSONOutput(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers/"+stubTriggerID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runCancel(cmd, cancelOptions{
		TriggerID:         stubTriggerID,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCancel: %v; stderr=%s", err, stderr.String())
	}
	var got map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if got["cancelled"] != true {
		t.Errorf("expected cancelled=true; got %+v", got)
	}
	if got["trigger_id"] != stubTriggerID {
		t.Errorf("expected trigger_id echoed; got %+v", got)
	}
}

func TestRunCancel404SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers/"+stubTriggerID,
		func(w http.ResponseWriter, _ *http.Request) {
			// No Content-Type — mirrors the FastAPI HTTPException
			// wire shape and the approvals-sibling test pattern.
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"trigger_not_found"}`))
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCancel(cmd, cancelOptions{
		TriggerID:         stubTriggerID,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 404 error")
	}
	if !strings.Contains(stderr.String(), "trigger_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

func TestRunCancel409SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/scheduler/triggers/"+stubTriggerID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"trigger_already_fired"}`))
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCancel(cmd, cancelOptions{
		TriggerID:         stubTriggerID,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 409 error")
	}
	if !strings.Contains(stderr.String(), "trigger_already_fired") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

func TestRunCancelRejectsInvalidTriggerUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCancel(cmd, cancelOptions{TriggerID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "trigger-id is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// printTriggerSummary
// ---------------------------------------------------------------

func TestPrintTriggerSummaryNilNoop(t *testing.T) {
	var buf bytes.Buffer
	printTriggerSummary(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("expected nil trigger to render nothing; got %q", buf.String())
	}
}

func TestPrintTriggerSummaryHasAllKeys(t *testing.T) {
	trigger := fakeTrigger(t, "cron")
	cron := "0 12 * * *"
	trigger.CronExpr = &cron
	var buf bytes.Buffer
	printTriggerSummary(&buf, &trigger)
	out := buf.String()
	for _, want := range []string{"id:", "tenant_id:", "kind:", "status:", "cron_expr:"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in summary; got %q", want, out)
		}
	}
}
