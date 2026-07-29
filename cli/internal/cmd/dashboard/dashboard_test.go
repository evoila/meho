// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// seedXDGAndToken seeds a per-test config dir + token store (mirrors the
// sibling verb-tree tests). MEHO_KEYRING_DISABLE=1 forces the file-backed
// token store so the test never touches the OS keyring.
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
		dir+"/meho/config.json",
		auth.Config{BackplaneURL: backplaneURL},
	); err != nil {
		t.Fatalf("SaveConfigAt: %v", err)
	}
	return dir
}

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

const (
	stubDashboardID = "33333333-3333-3333-3333-333333333333"
	stubSensorID    = "11111111-1111-1111-1111-111111111111"
	stubTenantID    = "22222222-2222-2222-2222-222222222222"
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

// fakeMember builds a minimal api.DashboardMemberView fixture.
func fakeMember(t *testing.T) api.DashboardMemberView {
	t.Helper()
	return api.DashboardMemberView{
		SensorId:       parseStubUUID(t, stubSensorID),
		Name:           "disk-space",
		ConnectorId:    "vmware-rest-9.0",
		OpId:           "vmware.vm.list",
		RawState:       api.DashboardMemberViewRawState("ok"),
		EffectiveState: api.DashboardMemberViewEffectiveState("ok"),
		Pending:        false,
		Severity:       api.SensorSeverity("critical"),
		ForSeconds:     0,
		Status:         api.SensorStatus("active"),
	}
}

// fakeDashboardRead builds a minimal api.DashboardRead fixture (list row).
func fakeDashboardRead(t *testing.T) api.DashboardRead {
	t.Helper()
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	return api.DashboardRead{
		Id:           parseStubUUID(t, stubDashboardID),
		TenantId:     parseStubUUID(t, stubTenantID),
		Name:         "prod-health",
		MemberCount:  1,
		State:        api.DashboardReadState("ok"),
		CreatedBySub: "alice@example.com",
		CreatedAt:    now,
		UpdatedAt:    now,
	}
}

// fakeDashboardDetail builds a minimal api.DashboardDetail fixture (detail).
func fakeDashboardDetail(t *testing.T) api.DashboardDetail {
	t.Helper()
	now := time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC)
	return api.DashboardDetail{
		Id:           parseStubUUID(t, stubDashboardID),
		TenantId:     parseStubUUID(t, stubTenantID),
		Name:         "prod-health",
		MemberCount:  1,
		State:        api.DashboardDetailState("ok"),
		CreatedBySub: "alice@example.com",
		CreatedAt:    now,
		UpdatedAt:    now,
		Members:      []api.DashboardMemberView{fakeMember(t)},
	}
}

// ---------------------------------------------------------------
// Subcommand wiring
// ---------------------------------------------------------------

func TestNewRootCmd_Subcommands(t *testing.T) {
	cmd := NewRootCmd()
	if cmd.Use != "dashboard" {
		t.Fatalf("expected Use=dashboard, got %q", cmd.Use)
	}
	want := map[string]bool{
		"list":                  true,
		"show <dashboard_id>":   true,
		"create":                true,
		"delete <dashboard_id>": true,
	}
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
// listQueryParams — wire-shape pin
// ---------------------------------------------------------------

func TestListQueryParamsOmitsEmptyFilters(t *testing.T) {
	got := listQueryParams(listOptions{}, nil)
	if got.TenantFilter != nil || got.Limit != nil || got.Offset != nil {
		t.Errorf("expected all filters nil; got %+v", got)
	}
}

func TestListQueryParamsForwardsFilters(t *testing.T) {
	tenantID := parseStubUUID(t, stubOtherTenant)
	got := listQueryParams(listOptions{Limit: 25, Offset: 50}, &tenantID)
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

func TestBuildCreateBodyMinimal(t *testing.T) {
	body := buildCreateBody(createOptions{Name: "prod-health"}, nil, nil)
	if body.Name != "prod-health" {
		t.Errorf("name not forwarded; got %+v", body)
	}
	// An empty member set must still forward a non-nil sensor_ids so the
	// zero-member rule applies deterministically.
	if body.SensorIds == nil {
		t.Errorf("expected non-nil sensor_ids for zero-member create; got nil")
	}
	if len(*body.SensorIds) != 0 {
		t.Errorf("expected empty sensor_ids; got %+v", *body.SensorIds)
	}
	if body.Description != nil {
		t.Errorf("description should stay nil when omitted; got %+v", body.Description)
	}
	if body.TenantId != nil {
		t.Errorf("tenant_id should stay nil when omitted; got %+v", body.TenantId)
	}
}

func TestBuildCreateBodyFull(t *testing.T) {
	tenantID := parseStubUUID(t, stubOtherTenant)
	sensorIDs := []openapi_types.UUID{parseStubUUID(t, stubSensorID)}
	body := buildCreateBody(
		createOptions{Name: "prod-health", Description: "prod glance"},
		sensorIDs, &tenantID,
	)
	if body.Description == nil || *body.Description != "prod glance" {
		t.Errorf("description not forwarded; got %+v", body.Description)
	}
	if body.SensorIds == nil || len(*body.SensorIds) != 1 ||
		(*body.SensorIds)[0].String() != stubSensorID {
		t.Errorf("sensor_ids not forwarded; got %+v", body.SensorIds)
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
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		// Serve the real generated envelope shape, not a bare array (#2383).
		_ = json.NewEncoder(w).Encode(api.DashboardListResponse{
			Dashboards: []api.DashboardRead{fakeDashboardRead(t)},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), stubDashboardID) {
		t.Errorf("expected dashboard id in stdout; got %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), "prod-health") {
		t.Errorf("expected dashboard name in stdout; got %q", stdout.String())
	}
}

func TestRunListJSONGolden(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.DashboardListResponse{
			Dashboards: []api.DashboardRead{fakeDashboardRead(t)},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	// The --json path must round-trip back through the generated envelope
	// (proves the double decodes the real wire shape, not a bare array).
	var got api.DashboardListResponse
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout not a DashboardListResponse: %v; %q", err, stdout.String())
	}
	if len(got.Dashboards) != 1 || got.Dashboards[0].Id.String() != stubDashboardID {
		t.Errorf("unexpected --json envelope; got %+v", got)
	}
	if string(got.Dashboards[0].State) != "ok" || got.Dashboards[0].MemberCount != 1 {
		t.Errorf("rollup/member_count not round-tripped; got %+v", got.Dashboards[0])
	}
}

func TestRunListEmptyResponse(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.DashboardListResponse{Dashboards: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no dashboards") {
		t.Errorf("expected empty-list message; got %q", stdout.String())
	}
}

func TestRunListForwardsFilters(t *testing.T) {
	var capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, r *http.Request) {
		capturedQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.DashboardListResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{
		Tenant:            stubOtherTenant,
		Limit:             25,
		Offset:            50,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{
		"tenant_filter=" + stubOtherTenant,
		"limit=25",
		"offset=50",
	} {
		if !strings.Contains(capturedQuery, want) {
			t.Errorf("expected query %q to contain %q", capturedQuery, want)
		}
	}
}

func TestRunListInvalidTenantFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Tenant: "not-a-uuid"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--tenant is not a valid UUID") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunListOverLimitFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Limit: 501}); err == nil {
		t.Fatalf("expected over-limit validation error")
	}
	if !strings.Contains(stderr.String(), "--limit must be between 1 and 500") {
		t.Errorf("expected limit message; got %q", stderr.String())
	}
}

func TestRunList200WithoutPayloadSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"dashboards":[]}`)) // no Content-Type → JSON200 nil
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err == nil {
		t.Fatalf("expected nil-guard error on missing payload")
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a dashboard list payload") {
		t.Errorf("expected nil-guard message; got %q", stderr.String())
	}
}

func TestRunList403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"cross_tenant_requires_platform_admin"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Tenant: stubOtherTenant, BackplaneOverride: srv.URL}); err == nil {
		t.Fatalf("expected 403 to surface as error")
	}
	if !strings.Contains(stderr.String(), "cross_tenant_requires_platform_admin") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// show verb
// ---------------------------------------------------------------

func TestRunShowHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet {
				t.Errorf("expected GET; got %s", r.Method)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(fakeDashboardDetail(t))
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runShow(cmd, showOptions{
		DashboardID:       stubDashboardID,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	// Rollup state + the member's raw/effective state must both render.
	for _, want := range []string{"state:", "member_count:", "SENSOR_ID", stubSensorID, "disk-space"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in show output; got %q", want, out)
		}
	}
}

func TestRunShowJSONGolden(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(fakeDashboardDetail(t))
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runShow(cmd, showOptions{
		DashboardID:       stubDashboardID,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	var got api.DashboardDetail
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout not a DashboardDetail: %v; %q", err, stdout.String())
	}
	if got.Id.String() != stubDashboardID || len(got.Members) != 1 {
		t.Errorf("unexpected --json detail; got %+v", got)
	}
	if got.Members[0].SensorId.String() != stubSensorID ||
		string(got.Members[0].RawState) != "ok" {
		t.Errorf("member breakdown not round-tripped; got %+v", got.Members[0])
	}
}

func TestRunShowZeroMemberRendersHint(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
		func(w http.ResponseWriter, _ *http.Request) {
			d := fakeDashboardDetail(t)
			d.Members = nil
			d.MemberCount = 0
			d.State = api.DashboardDetailState("unknown")
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(d)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runShow(cmd, showOptions{
		DashboardID:       stubDashboardID,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runShow: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no members") {
		t.Errorf("expected zero-member hint; got %q", stdout.String())
	}
}

func TestRunShow404SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"dashboard_not_found"}`))
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runShow(cmd, showOptions{
		DashboardID:       stubDashboardID,
		BackplaneOverride: srv.URL,
	}); err == nil {
		t.Fatalf("expected 404 error")
	}
	if !strings.Contains(stderr.String(), "dashboard_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

func TestRunShowRejectsInvalidUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runShow(cmd, showOptions{DashboardID: "not-a-uuid"}); err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "dashboard-id is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// create verb
// ---------------------------------------------------------------

func TestRunCreateHappyPath(t *testing.T) {
	var capturedBody api.DashboardCreate
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		if err := json.NewDecoder(r.Body).Decode(&capturedBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(fakeDashboardDetail(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runCreate(cmd, createOptions{
		Name:              "prod-health",
		Description:       "prod glance",
		SensorIDs:         []string{stubSensorID},
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if capturedBody.Name != "prod-health" {
		t.Errorf("expected name on wire; got %q", capturedBody.Name)
	}
	if capturedBody.SensorIds == nil || len(*capturedBody.SensorIds) != 1 ||
		(*capturedBody.SensorIds)[0].String() != stubSensorID {
		t.Errorf("expected sensor_ids on wire; got %+v", capturedBody.SensorIds)
	}
	if !strings.Contains(stdout.String(), "created dashboard") {
		t.Errorf("expected created-prose in stdout; got %q", stdout.String())
	}
}

func TestRunCreateZeroMemberHappyPath(t *testing.T) {
	var capturedBody api.DashboardCreate
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(&capturedBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		d := fakeDashboardDetail(t)
		d.Members = nil
		d.MemberCount = 0
		d.State = api.DashboardDetailState("unknown")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(d)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runCreate(cmd, createOptions{
		Name:              "empty-dash",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	// A zero-member create forwards an empty (non-nil) sensor_ids.
	if capturedBody.SensorIds == nil || len(*capturedBody.SensorIds) != 0 {
		t.Errorf("expected empty sensor_ids on wire; got %+v", capturedBody.SensorIds)
	}
	if !strings.Contains(stdout.String(), "no members") {
		t.Errorf("expected zero-member hint; got %q", stdout.String())
	}
}

func TestRunCreateRejectsInvalidSensorUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name:      "x",
		SensorIDs: []string{"not-a-uuid"},
	})
	if err == nil {
		t.Fatalf("expected sensor-id validation error")
	}
	if !strings.Contains(stderr.String(), "--sensor-id \"not-a-uuid\" is not a valid UUID") {
		t.Errorf("expected sensor-id UUID message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsInvalidTenantUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{Name: "x", Tenant: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected tenant validation error")
	}
	if !strings.Contains(stderr.String(), "--tenant is not a valid UUID") {
		t.Errorf("expected tenant UUID message; got %q", stderr.String())
	}
}

func TestRunCreate422SurfacesSensorNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"sensor_not_found"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name:              "prod-health",
		SensorIDs:         []string{stubSensorID},
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 422 error")
	}
	// The refusal path must surface cleanly, no stack trace.
	if !strings.Contains(stderr.String(), "sensor_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
	if strings.Contains(stderr.String(), "goroutine") || strings.Contains(stderr.String(), ".go:") {
		t.Errorf("422 leaked a stack trace: %q", stderr.String())
	}
}

func TestRunCreate409SurfacesConflict(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"dashboard_name_conflict"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{Name: "dupe", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected 409 error")
	}
	if !strings.Contains(stderr.String(), "dashboard_name_conflict") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// delete verb
// ---------------------------------------------------------------

func TestRunDeleteHappyPath(t *testing.T) {
	var capturedPath, capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
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
	if err := runDelete(cmd, deleteOptions{
		DashboardID:       stubDashboardID,
		Tenant:            stubOtherTenant,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runDelete: %v; stderr=%s", err, stderr.String())
	}
	if capturedPath != "/api/v1/checks/dashboards/"+stubDashboardID {
		t.Errorf("unexpected path: %q", capturedPath)
	}
	if !strings.Contains(capturedQuery, "tenant_filter="+stubOtherTenant) {
		t.Errorf("expected tenant_filter; got %q", capturedQuery)
	}
	if !strings.Contains(stdout.String(), "deleted dashboard "+stubDashboardID) {
		t.Errorf("expected deleted-prose; got %q", stdout.String())
	}
}

func TestRunDeleteJSONOutput(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{
		DashboardID:       stubDashboardID,
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runDelete: %v; stderr=%s", err, stderr.String())
	}
	var got map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &got); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if got["deleted"] != true {
		t.Errorf("expected deleted=true; got %+v", got)
	}
	if got["dashboard_id"] != stubDashboardID {
		t.Errorf("expected dashboard_id echoed; got %+v", got)
	}
}

func TestRunDelete404SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"dashboard_not_found"}`))
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{
		DashboardID:       stubDashboardID,
		BackplaneOverride: srv.URL,
	}); err == nil {
		t.Fatalf("expected 404 error")
	}
	if !strings.Contains(stderr.String(), "dashboard_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

func TestRunDeleteRejectsInvalidUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{DashboardID: "not-a-uuid"}); err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "dashboard-id is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// printDashboardSummary / sanitizeCell
// ---------------------------------------------------------------

func TestPrintDashboardSummaryNilNoop(t *testing.T) {
	var buf bytes.Buffer
	printDashboardSummary(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("expected nil dashboard to render nothing; got %q", buf.String())
	}
}

func TestPrintDashboardSummaryHasAllKeys(t *testing.T) {
	d := fakeDashboardDetail(t)
	var buf bytes.Buffer
	printDashboardSummary(&buf, &d)
	out := buf.String()
	for _, want := range []string{"id:", "tenant_id:", "name:", "state:", "member_count:", "created_at:"} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in summary; got %q", want, out)
		}
	}
}

func TestSanitizeCellNeutralizesControlChars(t *testing.T) {
	// An ANSI/CSI colour escape + CR + LF embedded in a persisted field
	// must not survive into a rendered table/summary cell.
	got := sanitizeCell("ok\x1b[31mRED\x1b[0m\rmore\n")
	if strings.ContainsAny(got, "\x1b\r\n") {
		t.Fatalf("control chars survived sanitize: %q", got)
	}
	const printable = "prod-health é 名前"
	if sanitizeCell(printable) != printable {
		t.Errorf("printable text altered: %q", sanitizeCell(printable))
	}
}
