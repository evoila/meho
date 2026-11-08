// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRenderHumanHealth_ServerTime: one line with the server's time in
// UTC (#5504), also for a login that chose no tenant; nothing new when
// an older backplane sends no field.
func TestRenderHumanHealth_ServerTime(t *testing.T) {
	render := func(resp *api.HealthResponse) string {
		var buf bytes.Buffer
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}
	chosen := func(ts *time.Time) *api.HealthResponse {
		return &api.HealthResponse{
			Operator:   api.OperatorIdentity{Sub: "alice-sub-id"},
			Vault:      api.VaultStatus{Reachable: true, ReadOk: true},
			Db:         api.DbStatus{Migrated: ptrBool(true)},
			ServerTime: ts,
		}
	}
	// A time in another zone is shown in UTC.
	zone := time.FixedZone("plus-two", 2*60*60)
	ts := time.Date(2026, 10, 31, 16, 34, 28, 0, zone)
	want := "  Server time: 2026-10-31T14:34:28Z\n"

	withTime := render(chosen(&ts))
	if !strings.Contains(withTime, want) {
		t.Errorf("missing %q in output:\n%s", want, withTime)
	}
	missing := render(chosen(nil))
	if strings.Contains(missing, "Server time") {
		t.Errorf("no line when the field is missing, got:\n%s", missing)
	}
	if strings.Count(withTime, "\n") != strings.Count(missing, "\n")+1 {
		t.Errorf("the time adds exactly one line:\n%s\n---\n%s", withTime, missing)
	}

	unchosen := render(&api.HealthResponse{
		Operator: api.OperatorIdentity{
			Sub:     "alice",
			Tenants: &[]api.TenantEntry{{Name: "tenant-a", TenantRole: api.TenantRoleReadOnly}},
		},
		Vault:      api.VaultStatus{Detail: ptrStr("tenant_not_chosen")},
		Db:         api.DbStatus{Migrated: ptrBool(true)},
		ServerTime: &ts,
	})
	if !strings.Contains(unchosen, want) {
		t.Errorf("missing %q in output for a login with no tenant chosen:\n%s", want, unchosen)
	}
}
