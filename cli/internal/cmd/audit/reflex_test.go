// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func f32(v float32) *float32 { return &v }

// TestBuildReflexParamsAlwaysSendsSinceOmitsRest — --since carries a
// CLI default so it is always emitted; --until / --tenant stay nil when
// unset so the backend applies its own defaults.
func TestBuildReflexParamsAlwaysSendsSinceOmitsRest(t *testing.T) {
	p := buildReflexParams(reflexOptions{Since: "7d"})
	if p.Since == nil || *p.Since != "7d" {
		t.Errorf("Since: got %v; want 7d", p.Since)
	}
	if p.Until != nil {
		t.Errorf("Until should be nil; got %v", *p.Until)
	}
	if p.TenantFilter != nil {
		t.Errorf("TenantFilter should be nil; got %v", *p.TenantFilter)
	}
}

// TestBuildReflexParamsEmitsUntilAndTenant — a set --until / --tenant
// pair lands on the params struct.
func TestBuildReflexParamsEmitsUntilAndTenant(t *testing.T) {
	tid := "00000000-0000-0000-0000-0000000000ff"
	p := buildReflexParams(reflexOptions{Since: "30d", Until: "1d", Tenant: tid})
	if p.Until == nil || *p.Until != "1d" {
		t.Errorf("Until: got %v; want 1d", p.Until)
	}
	if p.TenantFilter == nil || p.TenantFilter.String() != tid {
		t.Errorf("TenantFilter: got %v; want %s", p.TenantFilter, tid)
	}
}

// TestRunReflexRendersSurfaceTable — round-trip: the verb sends --since
// on the wire and renders both surfaces, with the CLI/REST client-side
// metrics reading `n/a`.
func TestRunReflexRendersSurfaceTable(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/reflex", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		if got := r.URL.Query().Get("since"); got != "30d" {
			t.Errorf("since: got %q; want 30d", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.ReflexReport{
			Since: mustTS(t, "2026-08-01T00:00:00Z"),
			Until: mustTS(t, "2026-08-20T00:00:00Z"),
			Surfaces: []api.SurfaceMetrics{
				{
					Surface:                   api.SurfaceMetricsSurfaceAgent,
					ReadBeforeActPct:          f32(50.0),
					ReadBeforeActReadFirst:    1,
					ReadBeforeActSessions:     2,
					AnnounceCoveragePct:       f32(50.0),
					AnnounceCoverageAnnounced: 1,
					AnnounceCoverageWriteOps:  2,
					WriteBackPer100CallOps:    f32(50.0),
					WriteBackAddCalls:         1,
					WriteBackCallOperations:   2,
				},
				{
					Surface:                   api.SurfaceMetricsSurfaceCliRest,
					ReadBeforeActPct:          nil,
					ReadBeforeActSessions:     0,
					AnnounceCoveragePct:       f32(0.0),
					AnnounceCoverageWriteOps:  1,
					AnnounceCoverageAnnounced: 0,
					WriteBackPer100CallOps:    nil,
				},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runReflex(cmd, reflexOptions{Since: "30d", BackplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runReflex: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	for _, want := range []string{
		"agent", "cli_rest", "read-before-act", "announce-coverage", "write-back rate",
		"50.00%", "n/a",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout missing %q in:\n%s", want, out)
		}
	}
}

// TestRunReflexJSONRoundTrips — --json emits the raw server bytes; the
// typed ReflexReport shape parses back cleanly.
func TestRunReflexJSONRoundTrips(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/reflex", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(api.ReflexReport{
			Since:    mustTS(t, "2026-08-01T00:00:00Z"),
			Until:    mustTS(t, "2026-08-20T00:00:00Z"),
			Surfaces: []api.SurfaceMetrics{},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runReflex(cmd, reflexOptions{Since: "7d", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runReflex --json: %v", err)
	}
	var decoded api.ReflexReport
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not valid JSON: %v\n%s", err, stdout.String())
	}
}

// TestRunReflexForbiddenSurfacesAsInsufficientRole — a 403 (cross-tenant
// gate) routes to the insufficient_role category, matching the sibling
// audit verbs.
func TestRunReflexForbiddenSurfacesAsInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/reflex", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"cross_tenant_requires_platform_admin"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, _ := newRunCmd(t)
	err := runReflex(cmd, reflexOptions{
		Since:             "7d",
		Tenant:            "00000000-0000-0000-0000-0000000000ff",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error for 403")
	}
}
