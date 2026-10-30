// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

const testTenantID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa5029"

func newTenantSettingTestCmd() (*cobra.Command, *bytes.Buffer) {
	cmd := &cobra.Command{}
	cmd.SetContext(context.Background())
	out := &bytes.Buffer{}
	cmd.SetOut(out)
	cmd.SetErr(&bytes.Buffer{})
	return cmd, out
}

func boolPtr(v bool) *bool { return &v }

// TestTenantSettingSetSendsBodyAndTenant checks that `set --off` PUTs
// {"enabled": false} to the op route with ?tenant_id and prints both values.
func TestTenantSettingSetSendsBodyAndTenant(t *testing.T) {
	var posted map[string]any
	var query string
	srv := mockBackplane(t, map[string]mockHandler{
		"": func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPut ||
				!strings.HasPrefix(r.URL.Path, "/api/v1/connectors/acme-ops-2/tenant-settings/operations/") {
				t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			}
			query = r.URL.RawQuery
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &posted)
			writeJSON(t, w, 200, api.TenantOperationSettingItem{
				OpId: "GET:/widgets", DefaultEnabled: true, TenantEnabled: boolPtr(false), Enabled: false,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, out := newTenantSettingTestCmd()
	err := runTenantSettingSet(cmd, "acme-ops-2", "GET:/widgets", false, boolPtr(false),
		tenantSettingFlags{tenantID: testTenantID, backplaneOverride: srv.URL})
	if err != nil {
		t.Fatalf("runTenantSettingSet: %v", err)
	}
	if posted["enabled"] != false {
		t.Errorf("body enabled = %v, want false", posted["enabled"])
	}
	if !strings.Contains(query, "tenant_id="+testTenantID) {
		t.Errorf("query %q does not name the tenant", query)
	}
	for _, want := range []string{"tenant setting is now off", "GET:/widgets", "on", "off"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output missing %q:\n%s", want, out.String())
		}
	}
}

// TestTenantSettingSetGroupDefaultSendsNull checks that `set-group --default`
// PUTs {"enabled": null} to the group route.
func TestTenantSettingSetGroupDefaultSendsNull(t *testing.T) {
	var posted map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"PUT /api/v1/connectors/acme-ops-2/tenant-settings/groups/widgets": func(w http.ResponseWriter, r *http.Request) {
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &posted)
			writeJSON(t, w, 200, api.TenantGroupSettingResponse{
				ConnectorId: "acme-ops-2",
				GroupKey:    "widgets",
				Items:       []api.TenantOperationSettingItem{{OpId: "GET:/widgets", DefaultEnabled: true, Enabled: true}},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, out := newTenantSettingTestCmd()
	if err := runTenantSettingSet(cmd, "acme-ops-2", "widgets", true, nil,
		tenantSettingFlags{backplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runTenantSettingSet: %v", err)
	}
	value, present := posted["enabled"]
	if !present || value != nil {
		t.Errorf("body must carry enabled=null, got %v (present=%v)", value, present)
	}
	if !strings.Contains(out.String(), "tenant setting is now default") {
		t.Errorf("output:\n%s", out.String())
	}
}

// TestTenantSettingShowPrintsBothValues checks that `show` lists the install
// default and the tenant setting of each operation.
func TestTenantSettingShowPrintsBothValues(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"GET /api/v1/connectors/acme-ops-2/tenant-settings": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, api.TenantOperationSettingsResponse{
				ConnectorId: "acme-ops-2",
				Items: []api.TenantOperationSettingItem{
					{OpId: "acme.widget.count", DefaultEnabled: false, TenantEnabled: boolPtr(true), Enabled: true},
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd, out := newTenantSettingTestCmd()
	if err := runTenantSettingShow(cmd, "acme-ops-2", tenantSettingFlags{backplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runTenantSettingShow: %v", err)
	}
	line := ""
	for _, l := range strings.Split(out.String(), "\n") {
		if strings.Contains(l, "acme.widget.count") {
			line = l
		}
	}
	if fields := strings.Fields(line); len(fields) != 4 || fields[1] != "off" || fields[2] != "on" || fields[3] != "on" {
		t.Errorf("want default=off tenant=on applies=on, got %q", line)
	}
}

func TestTenantSettingRejectsBadTenantID(t *testing.T) {
	cmd, _ := newTenantSettingTestCmd()
	err := runTenantSettingShow(cmd, "acme-ops-2", tenantSettingFlags{tenantID: "not-a-uuid"})
	if err == nil {
		t.Fatal("want an error for a bad --tenant-id")
	}
}

func TestPrintReviewOpsShowsTenantColumn(t *testing.T) {
	var buf bytes.Buffer
	printReviewOps(&buf, []api.ConnectorReviewOp{
		{OpId: "acme.widget.list", SafetyLevel: "safe", IsEnabled: true, TenantEnabled: boolPtr(false)},
		{OpId: "acme.widget.count", SafetyLevel: "safe", IsEnabled: true},
	})
	lines := strings.Split(buf.String(), "\n")
	if !strings.Contains(lines[0], "ten") {
		t.Fatalf("header has no ten column: %q", lines[0])
	}
	if fields := strings.Fields(lines[1]); fields[3] != "Y" || fields[4] != "." {
		t.Errorf("want en=Y ten=. for the op turned off, got %q", lines[1])
	}
	if fields := strings.Fields(lines[2]); fields[4] != "-" {
		t.Errorf("want ten=- for the op without a setting, got %q", lines[2])
	}
}
