// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestDescribeStatus_Vault: meho doctor reads the meho_status MCP
// answer. For a caller below the operator role the backplane runs no
// Vault check, and the summary says so instead of reachable=false.
func TestDescribeStatus_Vault(t *testing.T) {
	notChecked := false
	checked := true
	migrated := true
	cases := []struct {
		name  string
		vault api.VaultStatus
		want  string
	}{
		{
			name: "not checked for a lower role",
			vault: api.VaultStatus{
				Checked: &notChecked,
				Detail:  ptr("not checked: needs the operator role"),
			},
			want: "operator s; vault not checked (needs the operator role); db migrated=true",
		},
		{
			name:  "checked",
			vault: api.VaultStatus{Checked: &checked, Reachable: true, ReadOk: true},
			want:  "operator s; vault reachable=true read_ok=true; db migrated=true",
		},
		{
			name:  "older backplane sends no checked field",
			vault: api.VaultStatus{Reachable: false, ReadOk: false},
			want:  "operator s; vault reachable=false read_ok=false; db migrated=true",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := &api.HealthResponse{
				Operator: api.OperatorIdentity{Sub: "s"},
				Vault:    tc.vault,
				Db:       api.DbStatus{Migrated: &migrated},
			}
			if got := describeStatus(h); got != tc.want {
				t.Errorf("describeStatus() = %q, want %q", got, tc.want)
			}
		})
	}
}
