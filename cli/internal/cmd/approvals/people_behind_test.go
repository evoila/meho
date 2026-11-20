// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestPeopleBehindLine pins the "Behind it" line of `meho approvals show`:
// a person's request has none, a machine login with nobody known says
// "not known", and known people are grouped by why they count.
func TestPeopleBehindLine(t *testing.T) {
	ann := "Ann"
	people := []api.PersonBehindView{
		{Sub: "sub-a", Name: &ann, Why: "registered", WhyText: "Set up by"},
		{Sub: "sub-b", Why: "gave_grant", WhyText: "Grant by"},
		{Sub: "sub-c", Why: "gave_grant", WhyText: "Grant by"},
	}
	cases := []struct {
		name   string
		people *[]api.PersonBehindView
		known  *bool
		want   string
	}{
		{"person", nil, nil, ""},
		{"not known", &[]api.PersonBehindView{}, ptrBool(false), "Set up by: not known. Only an Admin may approve."},
		{"known", &people, ptrBool(true), "Set up by Ann (sub-a). Grant by sub-b, sub-c."},
	}
	for _, tc := range cases {
		if got := peopleBehindLine(tc.people, tc.known); got != tc.want {
			t.Errorf("%s: peopleBehindLine = %q; want %q", tc.name, got, tc.want)
		}
	}
}

// TestPrintDetailShowsPeopleBehind checks the line lands in the show view.
func TestPrintDetailShowsPeopleBehind(t *testing.T) {
	d := newApprovalView(t, "pending", "")
	d.PrincipalKind = ptr("agent")
	d.PeopleBehind = &[]api.PersonBehindView{{Sub: "sub-a", Why: "registered", WhyText: "Set up by"}}
	d.PeopleBehindKnown = ptrBool(true)

	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	if out := stdout.String(); !strings.Contains(out, "Behind it:    Set up by sub-a.") {
		t.Fatalf("show view has no people-behind line:\n%s", out)
	}
}

func ptrBool(v bool) *bool { return &v }
