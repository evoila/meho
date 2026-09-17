// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package serviceprincipal

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

const testGrantID = "11111111-1111-1111-1111-111111111111"

func TestNewGrantsCmdRegistersAllVerbs(t *testing.T) {
	cmd := NewGrantsCmd()
	names := map[string]bool{}
	for _, child := range cmd.Commands() {
		names[child.Name()] = true
	}
	for _, want := range []string{"list", "show", "create", "revoke"} {
		if !names[want] {
			t.Errorf("missing %s", want)
		}
	}
}

func TestBuildGrantCreateBody(t *testing.T) {
	body, err := buildGrantCreateBody("svc:automation", "vmware.vm.power", "vmware-rest-9.0", testGrantID, "", "scheduled power-on", "2026-10-01T00:00:00Z")
	if err != nil {
		t.Fatalf("buildGrantCreateBody: %v", err)
	}
	if body.TargetId == nil || body.TargetId.String() != testGrantID || body.ExpiresAt == nil || body.Reason != "scheduled power-on" {
		t.Errorf("unexpected body: %+v", body)
	}
	if !body.ExpiresAt.Equal(time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("expiry: %s", body.ExpiresAt)
	}
}

func TestBuildGrantCreateBodyTargetNameSelector(t *testing.T) {
	body, err := buildGrantCreateBody("svc:automation", "vmware.vm.power", "vmware-rest-9.0", "", "dc-*", "planned run", "")
	if err != nil {
		t.Fatal(err)
	}
	if body.TargetId != nil || body.TargetNamePattern == nil || *body.TargetNamePattern != "dc-*" {
		t.Errorf("selector body: %+v", body)
	}
}

func TestBuildGrantCreateBodyRejectsUnsafeInput(t *testing.T) {
	cases := []struct{ name, op, connector, target, pattern string }{
		{"glob operation", "vmware.*", "vmware-rest-9.0", "", ""},
		{"glob connector", "vmware.vm.power", "vmware-*", "", ""},
		{"delete operation", "DELETE:/vcenter/vm", "vmware-rest-9.0", "", ""},
		{"typed delete operation", "vmware.vm.delete", "vmware-rest-9.0", "", ""},
		{"composite operation", "vmware.composite.vm.power", "vmware-rest-9.0", "", ""},
		{"selector plus target", "vmware.vm.power", "vmware-rest-9.0", testGrantID, "dc-*"},
		{"invalid target", "vmware.vm.power", "vmware-rest-9.0", "not-a-uuid", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := buildGrantCreateBody("svc:automation", tc.op, tc.connector, tc.target, tc.pattern, "reason", ""); err == nil {
				t.Fatal("expected validation error")
			}
		})
	}
}

func TestGrantListParams(t *testing.T) {
	params := grantListParams("svc:automation", true)
	if params.PrincipalSub == nil || *params.PrincipalSub != "svc:automation" || params.IncludeRevoked == nil || !*params.IncludeRevoked {
		t.Errorf("params: %+v", params)
	}
}

func TestPrintGrantListEmpty(t *testing.T) {
	var output bytes.Buffer
	printGrantList(&output, nil)
	if !strings.Contains(output.String(), "no service-principal grants") {
		t.Errorf("output: %q", output.String())
	}
}

func TestRevokeDeclinesWithoutConfirmation(t *testing.T) {
	cmd := NewGrantsCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs([]string{"revoke", testGrantID, "--json"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("revoke decline: %v stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"status": "declined"`) {
		t.Errorf("output: %s", stdout.String())
	}
}
