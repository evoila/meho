// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestFormatVault_NotChecked: the backplane runs the Vault check only
// for the operator role and up. For a lower caller it sends
// checked=false, and the line says so instead of "unreachable".
func TestFormatVault_NotChecked(t *testing.T) {
	cases := []struct {
		name  string
		vault api.VaultStatus
		want  string
	}{
		{
			name: "needs the operator role",
			vault: api.VaultStatus{
				Checked: ptrBool(false),
				Detail:  ptrStr("not checked: needs the operator role"),
			},
			want: "not checked (needs the operator role)",
		},
		{
			name:  "no detail",
			vault: api.VaultStatus{Checked: ptrBool(false)},
			want:  "not checked",
		},
		{
			name:  "other reason",
			vault: api.VaultStatus{Checked: ptrBool(false), Detail: ptrStr("tenant_not_chosen")},
			want:  "not checked (tenant_not_chosen)",
		},
		{
			name: "checked and reachable",
			vault: api.VaultStatus{
				Checked: ptrBool(true), Reachable: true, ReadOk: true, Detail: ptrStr("version=3"),
			},
			want: "reachable, read OK (version=3)",
		},
		{
			name:  "checked and unreachable",
			vault: api.VaultStatus{Checked: ptrBool(true), Detail: ptrStr("login_failed: X")},
			want:  "unreachable (login_failed: X)",
		},
		{
			name:  "older backplane sends no checked field",
			vault: api.VaultStatus{Detail: ptrStr("login_failed: X")},
			want:  "unreachable (login_failed: X)",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := formatVault(tc.vault); got != tc.want {
				t.Errorf("formatVault() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestPrintHealth_VaultNotChecked(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "reader-sub"},
		Vault: api.VaultStatus{
			Checked: ptrBool(false),
			Detail:  ptrStr("not checked: needs the operator role"),
		},
		Db: api.DbStatus{Migrated: ptrBool(true)},
	}
	var buf bytes.Buffer
	if err := PrintHealth(&buf, resp); err != nil {
		t.Fatalf("PrintHealth: %v", err)
	}
	got := buf.String()
	if !strings.Contains(got, "Vault: not checked (needs the operator role)\n") {
		t.Errorf("missing the not-checked Vault line:\n%s", got)
	}
	if strings.Contains(got, "unreachable") {
		t.Errorf("a check that did not run must not read as unreachable:\n%s", got)
	}
}
