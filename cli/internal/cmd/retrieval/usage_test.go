// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

const testTenantUUID = "11111111-2222-3333-4444-555555555555"

// TestUsageQueryParamsDefaults — default --since=30d / --surface=all /
// no --tenant produces the canonical wire shape: the explicit
// `surface=all` rather than an omitted param (load-bearing for the
// backplane's audit + observability traces — they record the
// operator's intent rather than an absent param).
func TestUsageQueryParamsDefaults(t *testing.T) {
	params := usageQueryParams(usageOptions{Since: "30d", Surface: "all"})
	if params.Since == nil || *params.Since != "30d" {
		t.Errorf("since: got %v want 30d", params.Since)
	}
	if params.Surface == nil ||
		*params.Surface != api.UsageEndpointApiV1RetrieveUsageGetParamsSurfaceAll {
		t.Errorf("surface: got %v", params.Surface)
	}
	if params.TenantFilter != nil {
		t.Errorf("tenant_filter should be omitted when --tenant is unset; got %v",
			params.TenantFilter)
	}
}

// TestUsageQueryParamsTenantPropagated — --tenant <uuid> survives the
// params build verbatim and parses cleanly into the generated UUID
// type the backend route signature pins.
func TestUsageQueryParamsTenantPropagated(t *testing.T) {
	params := usageQueryParams(usageOptions{
		Since: "7d", Surface: "kb", Tenant: testTenantUUID,
	})
	if params.TenantFilter == nil {
		t.Fatalf("tenant_filter should be set when --tenant is non-empty")
	}
	if params.TenantFilter.String() != testTenantUUID {
		t.Errorf("tenant_filter: got %q want %q",
			params.TenantFilter.String(), testTenantUUID)
	}
	if params.Since == nil || *params.Since != "7d" {
		t.Errorf("since: got %v want 7d", params.Since)
	}
	if params.Surface == nil ||
		*params.Surface != api.UsageEndpointApiV1RetrieveUsageGetParamsSurfaceKb {
		t.Errorf("surface: got %v want kb", params.Surface)
	}
}

// TestUsageQueryParamsEmptyTenantOmitted — passing an empty --tenant
// value must NOT land as `tenant_filter=` on the wire (the
// backplane parser rejects the explicit-empty case with 422). The
// flag default is empty string, so this is the load-bearing common
// path: a bare `meho retrieval usage` invocation omits the param
// entirely.
func TestUsageQueryParamsEmptyTenantOmitted(t *testing.T) {
	params := usageQueryParams(usageOptions{
		Since: "30d", Surface: "all", Tenant: "",
	})
	if params.TenantFilter != nil {
		t.Errorf("empty tenant: tenant_filter should be omitted; got %q",
			params.TenantFilter.String())
	}
}

// TestUsageReportDecodesCanonical pins the wire shape: T5's
// UsageReport (backend Pydantic model + openapi.json snapshot)
// ships `since`/`until`/`surfaces`/`tenant_id`/`buckets`/
// `total_searches`, with `tenant_id` nullable. Decoding drift here
// surfaces as a Major-class wire-contract failure on the next
// `meho retrieval usage` round-trip. Validates that the generated
// `api.UsageReport` decodes the documented JSON shape cleanly.
func TestUsageReportDecodesCanonical(t *testing.T) {
	raw := []byte(`{
		"since": "2026-04-16T00:00:00Z",
		"until": "2026-05-16T00:00:00Z",
		"surfaces": ["kb", "memory", "operations"],
		"tenant_id": null,
		"buckets": [
			{
				"date": "2026-05-15",
				"surface": "kb",
				"search_count": 7,
				"distinct_operators": 3,
				"action_conversion_pct": 71.43
			},
			{
				"date": "2026-05-15",
				"surface": "memory",
				"search_count": 2,
				"distinct_operators": 1,
				"action_conversion_pct": 100.0
			}
		],
		"total_searches": 9
	}`)
	var got api.UsageReport
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode UsageReport: %v", err)
	}
	if got.TenantId != nil {
		t.Errorf("tenant_id: should decode nil for cross-tenant; got %v",
			got.TenantId)
	}
	if got.TotalSearches != 9 {
		t.Errorf("total_searches: got %d want 9", got.TotalSearches)
	}
	if len(got.Buckets) != 2 {
		t.Fatalf("buckets: got %d want 2", len(got.Buckets))
	}
	if got.Buckets[0].SearchCount != 7 || got.Buckets[0].DistinctOperators != 3 {
		t.Errorf("first bucket: %+v", got.Buckets[0])
	}
	// `action_conversion_pct` is `float32` on the generated type —
	// the decoder rounds 71.43 to the nearest float32. Compare with
	// a small epsilon to dodge the float-printing surprise.
	if got.Buckets[0].ActionConversionPct < 71.42 || got.Buckets[0].ActionConversionPct > 71.44 {
		t.Errorf("action_conversion_pct decode drifted: got %v",
			got.Buckets[0].ActionConversionPct)
	}
	if len(got.Surfaces) != 3 || got.Surfaces[0] != "kb" {
		t.Errorf("surfaces: got %v", got.Surfaces)
	}
}

// TestUsageReportDecodesTenantScoped — the tenant_admin-with-filter
// case ships a non-null tenant_id. The CLI must accept the same
// envelope shape and surface the tenant on the rendered header.
func TestUsageReportDecodesTenantScoped(t *testing.T) {
	raw := []byte(`{
		"since": "2026-04-16T00:00:00Z",
		"until": "2026-05-16T00:00:00Z",
		"surfaces": ["kb"],
		"tenant_id": "11111111-2222-3333-4444-555555555555",
		"buckets": [],
		"total_searches": 0
	}`)
	var got api.UsageReport
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode tenant-scoped UsageReport: %v", err)
	}
	if got.TenantId == nil {
		t.Fatalf("tenant_id should decode non-nil; got %v", got.TenantId)
	}
	if got.TenantId.String() != testTenantUUID {
		t.Errorf("tenant_id: got %q", got.TenantId.String())
	}
}

// TestPrintUsageTableEmptyBuckets — zero-bucket report renders the
// header lines + an explicit "no buckets" line so an operator
// running `meho retrieval usage` on a brand-new tenant doesn't think
// the verb hung silently.
func TestPrintUsageTableEmptyBuckets(t *testing.T) {
	since := time.Date(2026, 4, 16, 0, 0, 0, 0, time.UTC)
	until := time.Date(2026, 5, 16, 0, 0, 0, 0, time.UTC)
	r := &api.UsageReport{
		Since:    since,
		Until:    until,
		Surfaces: []string{"kb", "memory", "operations"},
		Buckets:  nil,
	}
	var buf bytes.Buffer
	printUsageTable(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"tenant: (operator's tenant)",
		"surfaces: kb,memory,operations",
		"2026-04-16T00:00:00Z → 2026-05-16T00:00:00Z",
		"total searches: 0",
		"(no buckets",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("empty render missing %q in:\n%s", want, out)
		}
	}
}

// TestPrintUsageTableHappyPath — typical report with buckets
// renders the header + the table rows. Buckets are sorted
// defensively: a backend that returns rows in `(surface, date)`
// order instead of `(date, surface)` would still produce a
// readable table because printUsageTable re-sorts. The test
// confirms that property by feeding rows out of order.
func TestPrintUsageTableHappyPath(t *testing.T) {
	var tenantUUID openapi_types.UUID
	if err := tenantUUID.UnmarshalText([]byte(testTenantUUID)); err != nil {
		t.Fatalf("parse tenant UUID: %v", err)
	}
	since := time.Date(2026, 4, 16, 0, 0, 0, 0, time.UTC)
	until := time.Date(2026, 5, 16, 0, 0, 0, 0, time.UTC)
	d14 := openapi_types.Date{Time: time.Date(2026, 5, 14, 0, 0, 0, 0, time.UTC)}
	d15 := openapi_types.Date{Time: time.Date(2026, 5, 15, 0, 0, 0, 0, time.UTC)}
	r := &api.UsageReport{
		Since:    since,
		Until:    until,
		Surfaces: []string{"kb", "memory"},
		TenantId: &tenantUUID,
		Buckets: []api.DailyUsageBucket{
			// Deliberately out-of-order: the renderer must sort.
			{Date: d15, Surface: "memory", SearchCount: 2, DistinctOperators: 1, ActionConversionPct: 100.0},
			{Date: d14, Surface: "kb", SearchCount: 5, DistinctOperators: 2, ActionConversionPct: 60.0},
			{Date: d15, Surface: "kb", SearchCount: 7, DistinctOperators: 3, ActionConversionPct: 71.43},
		},
		TotalSearches: 14,
	}
	var buf bytes.Buffer
	printUsageTable(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"tenant: " + testTenantUUID,
		"surfaces: kb,memory",
		"total searches: 14",
		"2026-05-14", "2026-05-15",
		"71.43",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("happy-path render missing %q in:\n%s", want, out)
		}
	}
	// Sort property: the first data row must be 2026-05-14 (kb),
	// not 2026-05-15. We locate the row prefix in the rendered
	// output and check ordering by index.
	if idx14, idx15 := strings.Index(out, "2026-05-14"), strings.Index(out, "2026-05-15"); idx14 == -1 || idx15 == -1 || idx14 > idx15 {
		t.Errorf("render should sort buckets ascending by date; got:\n%s", out)
	}
}

// TestSurfaceFlagValidatorRejectsUnknown — runUsage rejects an
// unknown --surface value before the network round-trip, with an
// unexpected_response StructuredError that names the valid set.
// Surfacing this client-side saves a network hop for the common
// typo case (`--surface kbb`, etc.).
func TestSurfaceFlagValidatorRejectsUnknown(t *testing.T) {
	cmd := newUsageCmd()
	cmd.SetArgs([]string{"--surface", "kbb"})
	var stderr bytes.Buffer
	cmd.SetErr(&stderr)
	cmd.SetOut(&bytes.Buffer{})
	err := cmd.Execute()
	if err == nil {
		t.Fatalf("expected validation error; got nil")
	}
	var ec output.ExitCoder
	if !errors.As(err, &ec) || ec.ExitCode() != output.ExitUnexpected {
		t.Fatalf("expected ExitUnexpected; got %T %v", err, err)
	}
	if !strings.Contains(stderr.String(), "--surface") ||
		!strings.Contains(stderr.String(), "kb | memory | operations | all") {
		t.Errorf("error should name valid set; got:\n%s", stderr.String())
	}
}

// ---------- HTTP wire shape (mocked) ----------

// TestGetUsageRoundTripWithMockServer — pins the wire contract
// between T5 (backend) and T5b (this CLI). The CLI GETs
// /api/v1/retrieve/usage, validates the query params land verbatim,
// and decodes the canonical UsageReport via the generated typed
// client. Used for the JSON contract sanity check that doesn't
// require a live backplane — true end-to-end coverage lives in the
// acceptance smoke against a real backplane.
func TestGetUsageRoundTripWithMockServer(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/usage": func(w http.ResponseWriter, r *http.Request) {
			if got := r.URL.Query().Get("since"); got != "30d" {
				t.Errorf("since: got %q want 30d", got)
			}
			if got := r.URL.Query().Get("surface"); got != "all" {
				t.Errorf("surface: got %q want all", got)
			}
			if r.URL.Query().Has("tenant_filter") {
				t.Errorf("tenant_filter should be absent on default call")
			}
			writeUsageJSON(t, w, 200, api.UsageReport{
				Since:    time.Date(2026, 4, 16, 0, 0, 0, 0, time.UTC),
				Until:    time.Date(2026, 5, 16, 0, 0, 0, 0, time.UTC),
				Surfaces: []string{"kb", "memory", "operations"},
				Buckets: []api.DailyUsageBucket{
					{
						Date:                openapi_types.Date{Time: time.Date(2026, 5, 15, 0, 0, 0, 0, time.UTC)},
						Surface:             "kb",
						SearchCount:         3,
						DistinctOperators:   2,
						ActionConversionPct: 66.67,
					},
				},
				TotalSearches: 3,
			})
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	resp, err := getUsage(context.Background(), srv.URL, usageOptions{
		Since: "30d", Surface: "all",
	})
	if err != nil {
		t.Fatalf("getUsage: %v", err)
	}
	if resp.StatusCode() != http.StatusOK {
		t.Fatalf("status: got %d want 200", resp.StatusCode())
	}
	if resp.JSON200 == nil {
		t.Fatalf("JSON200 should be populated on a JSON 200 response")
	}
	if resp.JSON200.TotalSearches != 3 || len(resp.JSON200.Buckets) != 1 {
		t.Fatalf("unexpected report: %+v", resp.JSON200)
	}
	if resp.JSON200.Buckets[0].SearchCount != 3 {
		t.Errorf("bucket: got %+v", resp.JSON200.Buckets[0])
	}
}

// TestGetUsage403MapsToInsufficientRole — backplane 403 (the
// tenant_filter_requires_tenant_admin case) propagates as a
// typed response with StatusCode=403; renderHTTPStatus routes it
// to insufficient_role with the body surfaced. The operator gets
// the "ask tenant_admin for the role grant" hint rather than a
// generic "network down" fallback. Load-bearing for issue #464
// acceptance criterion 3.
func TestGetUsage403MapsToInsufficientRole(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/usage": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Get("tenant_filter") == "" {
				t.Errorf("tenant_filter should be present on the role-rejected call")
			}
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"detail":"tenant_filter_requires_tenant_admin"}`))
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUsage(cmd, usageOptions{
		Since: "30d", Surface: "all",
		Tenant:            testTenantUUID,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 403 error; got nil")
	}
	if !strings.Contains(stderr.String(), "insufficient_role") {
		t.Errorf("expected insufficient_role classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "tenant_filter_requires_tenant_admin") {
		t.Errorf("403 body should carry the backplane detail; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5 (insufficient_role); got %v", err)
	}
}

// TestGetUsage400MapsToUnexpected — backplane 400 (the
// malformed --since case) propagates as a typed response with
// StatusCode=400; renderHTTPStatus routes it to
// unexpected_response with the body surfaced verbatim so the
// operator sees the actionable backend hint ("not a valid
// duration"). Load-bearing for issue #464 acceptance criterion 6.
func TestGetUsage400MapsToUnexpected(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/usage": func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"detail":"invalid since: 'tomorrow' is not a valid duration"}`))
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUsage(cmd, usageOptions{
		Since: "tomorrow", Surface: "all",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected 400 error; got nil")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "not a valid duration") {
		t.Errorf("400 body should carry the backplane detail; got %q", stderr.String())
	}
}

// TestRunUsageRejects200WithoutJSONPayload pins the JSON200
// nil-guard for the usage verb. A 200 with a missing or mistyped
// Content-Type leaves resp.JSON200 nil; without the guard,
// printUsageTable's nil-or-empty branch prints "(no buckets — zero
// searches in the window)" — actively misleading (conflated with a
// genuinely-empty window). Route to output.Unexpected (exit 4)
// instead.
func TestRunUsageRejects200WithoutJSONPayload(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/usage": func(w http.ResponseWriter, _ *http.Request) {
			// Deliberately omit Content-Type so the generated parser
			// leaves JSON200 nil.
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("not-json"))
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runUsage(cmd, usageOptions{
		Since: "30d", Surface: "all", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 200 without JSON payload")
	}
	if !strings.Contains(stderr.String(), "unexpected_response") {
		t.Errorf("expected unexpected_response classification; got %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "HTTP 200 without a usage report payload") {
		t.Errorf("expected detail mentioning missing payload; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}

// ---------- httptest helpers (local copies to avoid an export
// boundary between sibling test files — same pattern as the
// connector / operation packages, which each ship their own copy
// rather than reach into a shared test helper that would need to
// live one level up in the cmd tree).

type mockUsageHandler = http.HandlerFunc

// mockUsageBackplane stands up an httptest.Server that routes by
// `<METHOD> <path>` keys. Tests are responsible for calling Close()
// via defer.
func mockUsageBackplane(t *testing.T, routes map[string]mockUsageHandler) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := r.Method + " " + r.URL.Path
		if h, ok := routes[key]; ok {
			h(w, r)
			return
		}
		if h, ok := routes[""]; ok {
			h(w, r)
			return
		}
		t.Errorf("mockUsageBackplane: unhandled route %s", key)
		w.WriteHeader(404)
	}))
}

func writeUsageJSON(t *testing.T, w http.ResponseWriter, status int, body any) {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Errorf("writeUsageJSON marshal: %v", err)
		w.WriteHeader(500)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if _, err := w.Write(raw); err != nil {
		t.Errorf("writeUsageJSON write: %v", err)
	}
}

// primeUsageToken installs an in-memory token store with a usable
// bearer for the mocked backplane URL. Uses XDG_CONFIG_HOME + the
// file-backed token store fallback so the test stays
// keychain-free across CI environments. Mirrors the connector
// sibling's primeToken — duplicated rather than promoted to a
// package-level helper because the cmd/{connector,retrieval}
// packages can't import each other without an import cycle.
func primeUsageToken(t *testing.T, backplaneURL string) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", dir)
	t.Setenv("MEHO_KEYRING_DISABLE", "1")
	cfg := filepath.Join(dir, "meho", "config.json")
	if err := os.MkdirAll(filepath.Dir(cfg), 0o700); err != nil {
		t.Fatalf("mkdir config: %v", err)
	}
	cfgBlob, _ := json.Marshal(map[string]string{"backplane_url": backplaneURL})
	if err := os.WriteFile(cfg, cfgBlob, 0o600); err != nil {
		t.Fatalf("write config: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	store, err := auth.NewTokenStore()
	if err != nil {
		t.Fatalf("NewTokenStore: %v", err)
	}
	if err := store.Save(service, user, auth.StoredToken{
		AccessToken:  "test-bearer",
		BackplaneURL: backplaneURL,
	}); err != nil {
		t.Fatalf("store.Save: %v", err)
	}
}
