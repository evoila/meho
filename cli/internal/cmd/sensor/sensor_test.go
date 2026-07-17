// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

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
	stubSensorID    = "11111111-1111-1111-1111-111111111111"
	stubTenantID    = "22222222-2222-2222-2222-222222222222"
	stubOtherTenant = "44444444-4444-4444-4444-444444444444"
	stubAssertion   = `{"select":{"path":"$.count"},"compare":{"type":"threshold","op":"lt","critical":10}}`
)

func parseStubUUID(t *testing.T, s string) openapi_types.UUID {
	t.Helper()
	id, err := uuid.Parse(s)
	if err != nil {
		t.Fatalf("uuid.Parse(%q): %v", s, err)
	}
	return id
}

func mustAssertion(t *testing.T, raw string) api.AssertionSpec {
	t.Helper()
	var a api.AssertionSpec
	if err := json.Unmarshal([]byte(raw), &a); err != nil {
		t.Fatalf("unmarshal assertion: %v", err)
	}
	return a
}

// fakeSensor builds a minimal api.SensorRead fixture keyed off the stub
// UUIDs. Callers override fields per test.
func fakeSensor(t *testing.T, cadenceKind string) api.SensorRead {
	t.Helper()
	now := time.Date(2026, 7, 16, 12, 0, 0, 0, time.UTC)
	return api.SensorRead{
		Id:           parseStubUUID(t, stubSensorID),
		TenantId:     parseStubUUID(t, stubTenantID),
		Name:         "disk-space",
		ConnectorId:  "vmware-rest-9.0",
		OpId:         "vmware.vm.list",
		CadenceKind:  api.SensorCadenceKind(cadenceKind),
		Status:       api.SensorStatus("active"),
		Severity:     api.SensorSeverity("critical"),
		LastState:    api.SensorReadLastState("unknown"),
		ForSeconds:   0,
		IdentitySub:  "__sensor__",
		Timezone:     "UTC",
		Assertion:    map[string]any{"select": map[string]any{"path": "$.count"}},
		Params:       map[string]any{},
		CreatedBySub: "alice@example.com",
		CreatedAt:    now,
		UpdatedAt:    now,
	}
}

// ---------------------------------------------------------------
// Subcommand wiring + enum guards
// ---------------------------------------------------------------

func TestNewRootCmd_Subcommands(t *testing.T) {
	cmd := NewRootCmd()
	if cmd.Use != "sensor" {
		t.Fatalf("expected Use=sensor, got %q", cmd.Use)
	}
	want := map[string]bool{"list": true, "create": true, "delete <sensor_id>": true}
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

func TestValidCadenceKinds(t *testing.T) {
	for _, k := range []string{"interval", "cron"} {
		if !validCadenceKinds[k] {
			t.Errorf("expected %q to be a valid cadence kind", k)
		}
	}
	if validCadenceKinds["bogus"] {
		t.Errorf("expected 'bogus' to not be a valid cadence kind")
	}
}

func TestValidStatuses(t *testing.T) {
	for _, s := range []string{"active", "paused"} {
		if !validStatuses[s] {
			t.Errorf("expected %q to be a valid status", s)
		}
	}
}

func TestValidSeverities(t *testing.T) {
	for _, s := range []string{"degraded", "critical"} {
		if !validSeverities[s] {
			t.Errorf("expected %q to be a valid severity", s)
		}
	}
}

// ---------------------------------------------------------------
// JSON-object flag loader
// ---------------------------------------------------------------

func TestLoadJSONObjectBytesRejectsJSONNull(t *testing.T) {
	cmd := &cobra.Command{}
	cmd.SetIn(strings.NewReader(""))
	out, err := loadJSONObjectBytes(cmd, "null", "--assertion")
	if err == nil {
		t.Fatalf("expected error for JSON null, got out=%v err=nil", out)
	}
	if !strings.Contains(err.Error(), "got null") {
		t.Errorf("expected error to mention 'got null', got: %v", err)
	}
}

func TestLoadJSONObjectBytesRejectsOverCapFile(t *testing.T) {
	original := readJSONFile
	t.Cleanup(func() { readJSONFile = original })
	readJSONFile = func(_ string) ([]byte, error) {
		return nil, &capExceededError{}
	}
	cmd := &cobra.Command{}
	cmd.SetIn(bytes.NewReader(nil))
	out, err := loadJSONObjectBytes(cmd, "@/tmp/huge.json", "--assertion")
	if err == nil {
		t.Fatalf("expected over-cap file to surface an error, got out=%v err=nil", out)
	}
}

type capExceededError struct{}

func (capExceededError) Error() string {
	return "file \"/tmp/huge.json\" exceeds 262144-byte cap"
}

// ---------------------------------------------------------------
// listQueryParams — wire-shape pin
// ---------------------------------------------------------------

func TestListQueryParamsOmitsEmptyFilters(t *testing.T) {
	got := listQueryParams(listOptions{}, nil)
	if got.Status != nil || got.CadenceKind != nil || got.TenantFilter != nil ||
		got.Limit != nil || got.Offset != nil {
		t.Errorf("expected all filters nil; got %+v", got)
	}
}

func TestListQueryParamsForwardsFilters(t *testing.T) {
	tenantID := parseStubUUID(t, stubOtherTenant)
	got := listQueryParams(listOptions{
		Status:      "active",
		CadenceKind: "interval",
		Limit:       25,
		Offset:      50,
	}, &tenantID)
	if got.Status == nil || string(*got.Status) != "active" {
		t.Errorf("expected status=active forwarded; got %+v", got.Status)
	}
	if got.CadenceKind == nil || string(*got.CadenceKind) != "interval" {
		t.Errorf("expected cadence_kind=interval forwarded; got %+v", got.CadenceKind)
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

func TestBuildCreateBodyInterval(t *testing.T) {
	body := buildCreateBody(
		createOptions{
			Name:            "disk",
			ConnectorID:     "vmware-rest-9.0",
			OpID:            "vmware.vm.list",
			CadenceKind:     "interval",
			IntervalSeconds: 60,
			Severity:        "degraded",
			ForSeconds:      300,
			IdentitySub:     "svc",
		},
		mustAssertion(t, stubAssertion), nil, nil, nil,
	)
	if body.Name != "disk" || body.ConnectorId != "vmware-rest-9.0" || body.OpId != "vmware.vm.list" {
		t.Errorf("core fields not forwarded; got %+v", body)
	}
	if body.CadenceKind != api.SensorCadenceKind("interval") {
		t.Errorf("cadence_kind: got %q want interval", body.CadenceKind)
	}
	if body.IntervalSeconds == nil || *body.IntervalSeconds != 60 {
		t.Errorf("interval_seconds not forwarded; got %+v", body.IntervalSeconds)
	}
	if body.CronExpr != nil {
		t.Errorf("cron_expr should stay nil for interval; got %+v", body.CronExpr)
	}
	if body.Severity == nil || string(*body.Severity) != "degraded" {
		t.Errorf("severity not forwarded; got %+v", body.Severity)
	}
	if body.ForSeconds == nil || *body.ForSeconds != 300 {
		t.Errorf("for_seconds not forwarded; got %+v", body.ForSeconds)
	}
	if body.IdentitySub == nil || *body.IdentitySub != "svc" {
		t.Errorf("identity_sub not forwarded; got %+v", body.IdentitySub)
	}
}

func TestBuildCreateBodyCron(t *testing.T) {
	tenantID := parseStubUUID(t, stubOtherTenant)
	target := map[string]any{"target_id": "abc"}
	params := map[string]any{"limit": 10}
	body := buildCreateBody(
		createOptions{
			Name:        "nightly",
			ConnectorID: "vmware-rest-9.0",
			OpID:        "vmware.vm.list",
			CadenceKind: "cron",
			CronExpr:    "0 9 * * *",
			Timezone:    "Europe/Sarajevo",
		},
		mustAssertion(t, stubAssertion), &tenantID, target, params,
	)
	if body.CronExpr == nil || *body.CronExpr != "0 9 * * *" {
		t.Errorf("cron_expr not forwarded; got %+v", body.CronExpr)
	}
	if body.IntervalSeconds != nil {
		t.Errorf("interval_seconds should stay nil for cron; got %+v", body.IntervalSeconds)
	}
	if body.Timezone == nil || *body.Timezone != "Europe/Sarajevo" {
		t.Errorf("timezone not forwarded; got %+v", body.Timezone)
	}
	if body.Target == nil || (*body.Target)["target_id"] != "abc" {
		t.Errorf("target not forwarded; got %+v", body.Target)
	}
	if body.Params == nil {
		t.Errorf("params not forwarded; got nil")
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
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("expected GET; got %s", r.Method)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SensorListResponse{
			Sensors: []api.SensorRead{fakeSensor(t, "interval")},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), stubSensorID) {
		t.Errorf("expected sensor id in stdout; got %q", stdout.String())
	}
}

func TestRunListEmptyResponse(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SensorListResponse{Sensors: nil})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no sensors") {
		t.Errorf("expected empty-list message; got %q", stdout.String())
	}
}

func TestRunListForwardsFilters(t *testing.T) {
	var capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, r *http.Request) {
		capturedQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.SensorListResponse{})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{
		Status:            "active",
		CadenceKind:       "cron",
		Tenant:            stubOtherTenant,
		Limit:             25,
		Offset:            50,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runList: %v; stderr=%s", err, stderr.String())
	}
	for _, want := range []string{
		"status=active",
		"cadence_kind=cron",
		"tenant_filter=" + stubOtherTenant,
		"limit=25",
		"offset=50",
	} {
		if !strings.Contains(capturedQuery, want) {
			t.Errorf("expected query %q to contain %q", capturedQuery, want)
		}
	}
}

func TestRunListInvalidStatusFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{Status: "bogus"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--status must be one of") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunList200WithoutPayloadSurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"sensors":[]}`)) // no Content-Type → JSON200 nil
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runList(cmd, listOptions{BackplaneOverride: srv.URL}); err == nil {
		t.Fatalf("expected nil-guard error on missing payload")
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a sensor list payload") {
		t.Errorf("expected nil-guard message; got %q", stderr.String())
	}
}

func TestRunList403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, _ *http.Request) {
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
// create verb
// ---------------------------------------------------------------

func TestRunCreateIntervalHappyPath(t *testing.T) {
	var capturedBody api.SensorCreate
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		if err := json.NewDecoder(r.Body).Decode(&capturedBody); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(fakeSensor(t, "interval"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runCreate(cmd, createOptions{
		Name:              "disk",
		ConnectorID:       "vmware-rest-9.0",
		OpID:              "vmware.vm.list",
		AssertionArg:      stubAssertion,
		CadenceKind:       "interval",
		IntervalSeconds:   60,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runCreate: %v; stderr=%s", err, stderr.String())
	}
	if string(capturedBody.CadenceKind) != "interval" {
		t.Errorf("expected wire cadence_kind=interval; got %q", capturedBody.CadenceKind)
	}
	if capturedBody.IntervalSeconds == nil || *capturedBody.IntervalSeconds != 60 {
		t.Errorf("expected interval_seconds on wire; got %+v", capturedBody.IntervalSeconds)
	}
	if !strings.Contains(stdout.String(), "created sensor") {
		t.Errorf("expected created-prose in stdout; got %q", stdout.String())
	}
}

func TestRunCreateRejectsUnknownCadence(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runCreate(cmd, createOptions{CadenceKind: "bogus"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--cadence-kind must be one of") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsIntervalWithoutSeconds(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", ConnectorID: "c-1.0", OpID: "o", AssertionArg: stubAssertion,
		CadenceKind: "interval",
	})
	if err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--cadence-kind=interval requires --interval-seconds") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsNegativeIntervalSeconds(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", ConnectorID: "c-1.0", OpID: "o", AssertionArg: stubAssertion,
		CadenceKind: "interval", IntervalSeconds: -5,
	})
	if err == nil {
		t.Fatalf("expected validation error for negative --interval-seconds")
	}
	if !strings.Contains(stderr.String(), "--interval-seconds must be non-negative") {
		t.Errorf("expected non-negative message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsNegativeForSeconds(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", ConnectorID: "c-1.0", OpID: "o", AssertionArg: stubAssertion,
		CadenceKind: "interval", IntervalSeconds: 60, ForSeconds: -1,
	})
	if err == nil {
		t.Fatalf("expected validation error for negative --for-seconds")
	}
	if !strings.Contains(stderr.String(), "--for-seconds must be non-negative") {
		t.Errorf("expected non-negative message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsInvalidTenantUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", ConnectorID: "c-1.0", OpID: "o", AssertionArg: stubAssertion,
		CadenceKind: "cron", CronExpr: "0 9 * * *", Tenant: "not-a-uuid",
	})
	if err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "--tenant is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

func TestRunCreateRejectsMalformedAssertion(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", ConnectorID: "c-1.0", OpID: "o",
		AssertionArg: "not json", CadenceKind: "interval", IntervalSeconds: 60,
	})
	if err == nil {
		t.Fatalf("expected assertion parse error")
	}
	if !strings.Contains(stderr.String(), "--assertion") {
		t.Errorf("expected assertion error; got %q", stderr.String())
	}
}

func TestRunCreate422SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnprocessableEntity)
		_, _ = w.Write([]byte(`{"detail":"sensor_requires_safe_operation"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runCreate(cmd, createOptions{
		Name: "x", ConnectorID: "vmware-rest-9.0", OpID: "vmware.vm.delete",
		AssertionArg: stubAssertion, CadenceKind: "interval", IntervalSeconds: 60,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 422 error")
	}
	if !strings.Contains(stderr.String(), "sensor_requires_safe_operation") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// delete verb
// ---------------------------------------------------------------

func TestRunDeleteHappyPath(t *testing.T) {
	var capturedPath, capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID,
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
		SensorID:          stubSensorID,
		Tenant:            stubOtherTenant,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runDelete: %v; stderr=%s", err, stderr.String())
	}
	if capturedPath != "/api/v1/sensors/"+stubSensorID {
		t.Errorf("unexpected path: %q", capturedPath)
	}
	if !strings.Contains(capturedQuery, "tenant_filter="+stubOtherTenant) {
		t.Errorf("expected tenant_filter; got %q", capturedQuery)
	}
	if !strings.Contains(stdout.String(), "deleted sensor "+stubSensorID) {
		t.Errorf("expected deleted-prose; got %q", stdout.String())
	}
}

func TestRunDeleteJSONOutput(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{
		SensorID:          stubSensorID,
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
	if got["sensor_id"] != stubSensorID {
		t.Errorf("expected sensor_id echoed; got %+v", got)
	}
}

func TestRunDelete404SurfacesUnexpected(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID,
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"sensor_not_found"}`))
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{
		SensorID:          stubSensorID,
		BackplaneOverride: srv.URL,
	}); err == nil {
		t.Fatalf("expected 404 error")
	}
	if !strings.Contains(stderr.String(), "sensor_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}

func TestRunDeleteRejectsInvalidSensorUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runDelete(cmd, deleteOptions{SensorID: "not-a-uuid"}); err == nil {
		t.Fatalf("expected UUID validation error")
	}
	if !strings.Contains(stderr.String(), "sensor-id is not a valid UUID") {
		t.Errorf("expected UUID message; got %q", stderr.String())
	}
}

// ---------------------------------------------------------------
// printSensorSummary
// ---------------------------------------------------------------

func TestPrintSensorSummaryNilNoop(t *testing.T) {
	var buf bytes.Buffer
	printSensorSummary(&buf, nil)
	if buf.Len() != 0 {
		t.Errorf("expected nil sensor to render nothing; got %q", buf.String())
	}
}

func TestPrintSensorSummaryHasAllKeys(t *testing.T) {
	s := fakeSensor(t, "interval")
	interval := 60
	s.IntervalSeconds = &interval
	var buf bytes.Buffer
	printSensorSummary(&buf, &s)
	out := buf.String()
	for _, want := range []string{"id:", "tenant_id:", "name:", "op_id:", "status:", "last_state:", "severity:"} {
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
	// Ordinary printable text (including non-ASCII) is preserved verbatim.
	const printable = "disk-space é 名前"
	if sanitizeCell(printable) != printable {
		t.Errorf("printable text altered: %q", sanitizeCell(printable))
	}
}
