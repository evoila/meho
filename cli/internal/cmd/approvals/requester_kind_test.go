// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRequesterLabelMarksMachineKinds pins the requester marker (#3885):
// a machine login gets its kind in brackets; a person, a nil kind (a
// request made before the server saved it) and an unknown value get none.
func TestRequesterLabelMarksMachineKinds(t *testing.T) {
	name := "incident-triage"
	cases := []struct {
		kind     *string
		wantLine string
		wantScan string
	}{
		{ptr("agent"), "incident-triage (sa-sub) [agent]", "[agent] incident-triage"},
		{ptr("service"), "incident-triage (sa-sub) [service]", "[service] incident-triage"},
		{ptr("runner"), "incident-triage (sa-sub) [runner]", "[runner] incident-triage"},
		{ptr("user"), "incident-triage (sa-sub)", "incident-triage"},
		{nil, "incident-triage (sa-sub)", "incident-triage"},
		{ptr(""), "incident-triage (sa-sub)", "incident-triage"},
		{ptr("robot"), "incident-triage (sa-sub)", "incident-triage"},
	}
	for _, tc := range cases {
		if got := requesterLabel("sa-sub", &name, tc.kind); got != tc.wantLine {
			t.Errorf("requesterLabel(kind=%v) = %q; want %q", tc.kind, got, tc.wantLine)
		}
		if got := requesterScanLabel("sa-sub", &name, tc.kind); got != tc.wantScan {
			t.Errorf("requesterScanLabel(kind=%v) = %q; want %q", tc.kind, got, tc.wantScan)
		}
	}
	// No name: the marker still shows next to the bare sub.
	if got := requesterLabel("sa-sub", nil, ptr("agent")); got != "sa-sub [agent]" {
		t.Errorf("requesterLabel without name = %q; want %q", got, "sa-sub [agent]")
	}
}

// TestPrintDetailAndListMarkAgentRequester checks that show and list
// mark an agent requester (#3885), and a person stays unmarked.
func TestPrintDetailAndListMarkAgentRequester(t *testing.T) {
	d := newApprovalView(t, "pending", "")
	d.PrincipalName = ptr("incident-triage")
	d.PrincipalKind = ptr("agent")

	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	if out := stdout.String(); !strings.Contains(out, "Principal:    incident-triage (user-abc) [agent]") {
		t.Errorf("printDetail missing agent marker in:\n%s", out)
	}

	var buf bytes.Buffer
	printListTable(&buf, []api.ApprovalRequestView{d}, "", "", 0)
	if out := buf.String(); !strings.Contains(out, "[agent] incident-tr") {
		t.Errorf("printListTable missing agent marker in:\n%s", out)
	}

	person := newApprovalView(t, "pending", "")
	person.PrincipalName = ptr("Bob Requester")
	person.PrincipalKind = ptr("user")
	var buf2 bytes.Buffer
	printListTable(&buf2, []api.ApprovalRequestView{person}, "", "", 0)
	if out := buf2.String(); strings.Contains(out, "[user]") || !strings.Contains(out, "Bob Requester") {
		t.Errorf("printListTable person row wrong in:\n%s", out)
	}
}
