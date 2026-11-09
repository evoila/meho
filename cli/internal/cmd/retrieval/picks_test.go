// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"net/http"
	"strings"
	"testing"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/evoila/meho/cli/internal/api"
)

func picksReport() api.OperationPickReport {
	return api.OperationPickReport{
		TenantId: openapi_types.UUID{},
		Since:    time.Date(2026, 10, 9, 0, 0, 0, 0, time.UTC),
		Until:    time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC),
		Buckets: []api.OperationPickBucket{{
			Date:                   openapi_types.Date{Time: time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC)},
			ConnectorId:            "k8s-1.x",
			Searches:               4,
			SearchesFollowedByCall: 3,
			Calls:                  5,
			CallsInResults:         3,
			CallsAtRank1:           2,
			CallsInTop3:            3,
			CallsInResultsOk:       2,
			CallsNotInResults:      1,
			CallsUnknownOp:         1,
			CallsWithoutSearch:     1,
		}},
	}
}

func TestNewRootCmdRegistersPicks(t *testing.T) {
	for _, sub := range NewRootCmd().Commands() {
		if sub.Name() == "picks" {
			return
		}
	}
	t.Fatalf("subcommand picks not registered under `meho retrieval`")
}

func TestRunPicksSendsFlagsAndPrintsTable(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/operation-picks": func(w http.ResponseWriter, r *http.Request) {
			q := r.URL.Query()
			if q.Get("since") != "7d" || q.Get("connector_id") != "k8s-1.x" {
				t.Errorf("query: got %v", q)
			}
			if q.Get("tenant_filter") != testTenantUUID {
				t.Errorf("tenant_filter: got %q", q.Get("tenant_filter"))
			}
			writeUsageJSON(t, w, http.StatusOK, picksReport())
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runPicks(cmd, picksOptions{
		Since: "7d", Connector: "k8s-1.x", Tenant: testTenantUUID, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runPicks: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, "k8s-1.x") || !strings.Contains(out, "2026-11-07") {
		t.Errorf("table misses the bucket: %q", out)
	}
	if !strings.Contains(out, "no_search") {
		t.Errorf("table misses the header: %q", out)
	}
}

func TestRunPicksOmitsUnsetFilters(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/operation-picks": func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Query().Has("connector_id") || r.URL.Query().Has("tenant_filter") {
				t.Errorf("unset filters must not be sent: %v", r.URL.Query())
			}
			writeUsageJSON(t, w, http.StatusOK, api.OperationPickReport{Buckets: []api.OperationPickBucket{}})
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runPicks(cmd, picksOptions{Since: "30d", BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runPicks: %v", err)
	}
	if !strings.Contains(stdout.String(), "(no records in the window)") {
		t.Errorf("empty report: got %q", stdout.String())
	}
}

func TestRunPicksJSONPrintsTheReport(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/operation-picks": func(w http.ResponseWriter, _ *http.Request) {
			writeUsageJSON(t, w, http.StatusOK, picksReport())
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	if err := runPicks(cmd, picksOptions{Since: "30d", JSONOut: true, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runPicks: %v", err)
	}
	if !strings.Contains(stdout.String(), `"calls_in_results_ok": 2`) {
		t.Errorf("json output: got %q", stdout.String())
	}
}

func TestRunPicks403MapsToInsufficientRole(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/operation-picks": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"detail":"cross_tenant_requires_platform_admin"}`))
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runPicks(cmd, picksOptions{Since: "30d", Tenant: testTenantUUID, BackplaneOverride: srv.URL})
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
	if !strings.Contains(stderr.String(), "cross_tenant_requires_platform_admin") {
		t.Errorf("stderr: got %q", stderr.String())
	}
}

func TestRunPicksRejects200WithoutPayload(t *testing.T) {
	srv := mockUsageBackplane(t, map[string]mockUsageHandler{
		"GET /api/v1/retrieve/operation-picks": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("not-json"))
		},
	})
	defer srv.Close()
	primeUsageToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runPicks(cmd, picksOptions{Since: "30d", BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected an error on 200 without a report")
	}
	if strings.Contains(stdout.String(), "no records") {
		t.Errorf("a missing report must not read as empty: %q", stdout.String())
	}
	if !strings.Contains(stderr.String(), "without an operation picks report") {
		t.Errorf("stderr: got %q", stderr.String())
	}
}

func TestRunPicksRejectsANonUUIDTenant(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runPicks(cmd, picksOptions{Since: "30d", Tenant: "not-a-uuid"})
	if err == nil || !strings.Contains(stderr.String(), "not a UUID") {
		t.Errorf("expected a UUID error; got %v / %q", err, stderr.String())
	}
}
