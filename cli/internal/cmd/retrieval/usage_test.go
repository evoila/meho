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
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// TestBuildUsageURLDefaults — default --since=30d / --surface=all /
// no --tenant produces the canonical wire shape: the explicit
// `surface=all` rather than an omitted param (load-bearing for the
// backplane's audit + observability traces — they record the
// operator's intent rather than an absent param).
func TestBuildUsageURLDefaults(t *testing.T) {
	got := buildUsageURL("https://meho.test", usageOptions{
		Since: "30d", Surface: "all",
	})
	u, err := url.Parse(got)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if u.Path != "/api/v1/retrieve/usage" {
		t.Errorf("path: got %q want /api/v1/retrieve/usage", u.Path)
	}
	q := u.Query()
	if q.Get("since") != "30d" {
		t.Errorf("since: got %q", q.Get("since"))
	}
	if q.Get("surface") != "all" {
		t.Errorf("surface: got %q", q.Get("surface"))
	}
	if q.Has("tenant_filter") {
		t.Errorf("tenant_filter should be omitted when --tenant is unset")
	}
}

// TestBuildUsageURLTenantPropagated — --tenant <uuid> survives the
// URL build verbatim under the `tenant_filter` query-param name the
// backplane route signature pins.
func TestBuildUsageURLTenantPropagated(t *testing.T) {
	tenantUUID := "11111111-2222-3333-4444-555555555555"
	got := buildUsageURL("https://meho.test", usageOptions{
		Since: "7d", Surface: "kb", Tenant: tenantUUID,
	})
	u, err := url.Parse(got)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if u.Query().Get("tenant_filter") != tenantUUID {
		t.Errorf("tenant_filter: got %q want %q",
			u.Query().Get("tenant_filter"), tenantUUID)
	}
	if u.Query().Get("since") != "7d" {
		t.Errorf("since: got %q want 7d", u.Query().Get("since"))
	}
	if u.Query().Get("surface") != "kb" {
		t.Errorf("surface: got %q want kb", u.Query().Get("surface"))
	}
}

// TestBuildUsageURLEmptyTenantOmitted — passing an empty --tenant
// value must NOT land as `tenant_filter=` on the wire (the
// backplane parser rejects the explicit-empty case with 422). The
// flag default is empty string, so this is the load-bearing common
// path: a bare `meho retrieval usage` invocation omits the param
// entirely.
func TestBuildUsageURLEmptyTenantOmitted(t *testing.T) {
	got := buildUsageURL("https://meho.test", usageOptions{
		Since: "30d", Surface: "all", Tenant: "",
	})
	u, _ := url.Parse(got)
	if u.Query().Has("tenant_filter") {
		t.Errorf("empty tenant: tenant_filter should be omitted; got %q",
			u.Query().Get("tenant_filter"))
	}
}

// TestUsageReportDecodesCanonical pins the wire shape: T5's
// UsageReport (backend Pydantic model + openapi.json snapshot)
// ships `since`/`until`/`surfaces`/`tenant_id`/`buckets`/
// `total_searches`, with `tenant_id` nullable. Decoding drift here
// surfaces as a Major-class wire-contract failure on the next
// `meho retrieval usage` round-trip.
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
	var got UsageReport
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode UsageReport: %v", err)
	}
	if got.TenantID != nil {
		t.Errorf("tenant_id: should decode nil for cross-tenant; got %v",
			got.TenantID)
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
	if got.Buckets[0].ActionConversionPct != 71.43 {
		t.Errorf("action_conversion_pct decode lost precision: got %v",
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
	var got UsageReport
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("decode tenant-scoped UsageReport: %v", err)
	}
	if got.TenantID == nil || *got.TenantID == "" {
		t.Fatalf("tenant_id should decode non-nil; got %v", got.TenantID)
	}
	if *got.TenantID != "11111111-2222-3333-4444-555555555555" {
		t.Errorf("tenant_id: got %q", *got.TenantID)
	}
}

// TestPrintUsageTableEmptyBuckets — zero-bucket report renders the
// header lines + an explicit "no buckets" line so an operator
// running `meho retrieval usage` on a brand-new tenant doesn't think
// the verb hung silently.
func TestPrintUsageTableEmptyBuckets(t *testing.T) {
	r := &UsageReport{
		Since:    "2026-04-16T00:00:00Z",
		Until:    "2026-05-16T00:00:00Z",
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
	tenantUUID := "11111111-2222-3333-4444-555555555555"
	r := &UsageReport{
		Since:    "2026-04-16T00:00:00Z",
		Until:    "2026-05-16T00:00:00Z",
		Surfaces: []string{"kb", "memory"},
		TenantID: &tenantUUID,
		Buckets: []UsageBucket{
			// Deliberately out-of-order: the renderer must sort.
			{Date: "2026-05-15", Surface: "memory", SearchCount: 2, DistinctOperators: 1, ActionConversionPct: 100.0},
			{Date: "2026-05-14", Surface: "kb", SearchCount: 5, DistinctOperators: 2, ActionConversionPct: 60.0},
			{Date: "2026-05-15", Surface: "kb", SearchCount: 7, DistinctOperators: 3, ActionConversionPct: 71.43},
		},
		TotalSearches: 14,
	}
	var buf bytes.Buffer
	printUsageTable(&buf, r)
	out := buf.String()
	for _, want := range []string{
		"tenant: " + tenantUUID,
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

// TestUsageHTTPErrorString — Error() format pins the renderer's
// input shape (used by renderUsageError fallthrough and surfaced
// inside the unexpected_response detail).
func TestUsageHTTPErrorString(t *testing.T) {
	he := &usageHTTPError{StatusCode: 400, Body: "malformed since"}
	if he.Error() != "HTTP 400: malformed since" {
		t.Fatalf("usageHTTPError format: got %q", he.Error())
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
// and decodes the canonical UsageReport. Used for the JSON contract
// sanity check that doesn't require a live backplane — true
// end-to-end coverage lives in the acceptance smoke against a
// real backplane.
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
			writeUsageJSON(t, w, 200, UsageReport{
				Since:    "2026-04-16T00:00:00Z",
				Until:    "2026-05-16T00:00:00Z",
				Surfaces: []string{"kb", "memory", "operations"},
				Buckets: []UsageBucket{
					{Date: "2026-05-15", Surface: "kb", SearchCount: 3, DistinctOperators: 2, ActionConversionPct: 66.67},
				},
				TotalSearches: 3,
			})
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	got, err := getUsage(context.Background(), srv.URL, usageOptions{
		Since: "30d", Surface: "all",
	})
	if err != nil {
		t.Fatalf("getUsage: %v", err)
	}
	if got.TotalSearches != 3 || len(got.Buckets) != 1 {
		t.Fatalf("unexpected report: %+v", got)
	}
	if got.Buckets[0].SearchCount != 3 {
		t.Errorf("bucket: got %+v", got.Buckets[0])
	}
}

// TestGetUsage403MapsToInsufficientRole — backplane 403 (the
// tenant_filter_requires_tenant_admin case) propagates as a
// *usageHTTPError with StatusCode=403; renderUsageError routes it
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

	_, err := getUsage(context.Background(), srv.URL, usageOptions{
		Since: "30d", Surface: "all",
		Tenant: "11111111-2222-3333-4444-555555555555",
	})
	if err == nil {
		t.Fatalf("expected 403 error; got nil")
	}
	var he *usageHTTPError
	if !errors.As(err, &he) || he.StatusCode != 403 {
		t.Fatalf("expected *usageHTTPError 403; got %T %v", err, err)
	}
	if !strings.Contains(he.Body, "tenant_filter_requires_tenant_admin") {
		t.Errorf("403 body should carry the backplane detail; got %q", he.Body)
	}
}

// TestGetUsage400MapsToUnexpectedWithBody — backplane 400 (the
// malformed --since case) propagates as a *usageHTTPError with
// StatusCode=400; renderUsageError routes it to
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

	_, err := getUsage(context.Background(), srv.URL, usageOptions{
		Since: "tomorrow", Surface: "all",
	})
	if err == nil {
		t.Fatalf("expected 400 error; got nil")
	}
	var he *usageHTTPError
	if !errors.As(err, &he) || he.StatusCode != 400 {
		t.Fatalf("expected *usageHTTPError 400; got %T %v", err, err)
	}
	if !strings.Contains(he.Body, "not a valid duration") {
		t.Errorf("400 body should carry the backplane detail; got %q", he.Body)
	}
}

// TestGetUsageDecodeErrorClassifiable — a 200 OK with garbage JSON
// body must classify as JSON syntax (renderUsageError routes to
// unexpected_response). Without this branch a contract drift
// between T5 (backend) and T5b (CLI) would surface as
// "your network is down".
func TestGetUsageDecodeErrorClassifiable(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/usage": func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(200)
			// Malformed JSON — triggers a decode error.
			_, _ = w.Write([]byte(`{"since": "2026-`))
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	_, err := getUsage(context.Background(), srv.URL, usageOptions{
		Since: "30d", Surface: "all",
	})
	if err == nil {
		t.Fatalf("expected decode error; got nil")
	}
	var syntaxErr *json.SyntaxError
	var unmarshalErr *json.UnmarshalTypeError
	if !errors.As(err, &syntaxErr) &&
		!errors.As(err, &unmarshalErr) &&
		!errors.Is(err, http.ErrBodyReadAfterClose) {
		// The wrapped error should be a JSON syntax error. We
		// accept either the typed sentinel or
		// io.ErrUnexpectedEOF (the standard EOF surface for
		// truncated JSON streams).
		if !strings.Contains(err.Error(), "decode usage response") {
			t.Errorf("expected decode error wrap; got %T %v", err, err)
		}
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
