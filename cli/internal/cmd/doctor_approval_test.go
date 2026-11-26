// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

func signInHealth(state api.ApprovalSignInStatusState, detail string) *api.HealthResponse {
	s := &api.ApprovalSignInStatus{State: state, Client: "meho-cli-approve"}
	if detail != "" {
		s.Detail = &detail
	}
	return &api.HealthResponse{Approvals: &api.ApprovalsStatus{SignIn: s}}
}

func TestApprovalSignInCheck(t *testing.T) {
	cases := []struct {
		name       string
		health     *api.HealthResponse
		status     string
		detailHas  string
		hintHas    string
		hintAbsent bool
	}{
		{"ok", signInHealth(api.ApprovalSignInStatusStateOk, ""), doctorOK,
			`signs in through the login flow "meho approval sign-in level"`, "", true},
		{"flow missing", signInHealth(api.ApprovalSignInStatusStateFlowMissing, ""), doctorWarn,
			`no login flow "meho approval sign-in level"`, "keycloak.authflow.ensure_approval_flow", false},
		{"not bound", signInHealth(api.ApprovalSignInStatusStateNotBound, "the client meho-cli-approve has no own browser flow"), doctorWarn,
			"has no own browser flow", "keycloak.authflow.ensure_approval_flow", false},
		{"flow differs", signInHealth(api.ApprovalSignInStatusStateFlowDiffers, "the flow has 5 steps; wanted 4"), doctorWarn,
			"differs from MEHO's: the flow has 5 steps", "the level step's settings", false},
		{"direct grant on", signInHealth(api.ApprovalSignInStatusStateFlowDiffers, "the client meho-cli-approve allows direct grant"), doctorWarn,
			"differs from MEHO's: the client meho-cli-approve allows direct grant", "turns off direct grant and device grant", false},
		{"no right", signInHealth(api.ApprovalSignInStatusStateNotChecked, "no_right_to_read_login_flows"), doctorWarn,
			"could not check the approval sign-in: no_right_to_read_login_flows", "view-realm", false},
		{"unreachable", signInHealth(api.ApprovalSignInStatusStateNotChecked, "keycloak_unreachable"), doctorWarn,
			"keycloak_unreachable", "", true},
		{"unknown state", signInHealth("half_bound", "x"), doctorWarn, `unknown state "half_bound"`, "", true},
		{"older backplane", &api.HealthResponse{}, doctorWarn, "older version", "", true},
		{"no sign_in in the block", &api.HealthResponse{Approvals: &api.ApprovalsStatus{}}, doctorWarn, "older version", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := &doctorReport{}
			addApprovalSignInCheck(r, tc.health)
			c := findCheck(t, r, approvalSignInCheckName)
			if c.Layer != layerAuthed || c.Status != tc.status {
				t.Errorf("layer/status = %s/%s, want %s/%s", c.Layer, c.Status, layerAuthed, tc.status)
			}
			if !strings.Contains(c.Detail, tc.detailHas) {
				t.Errorf("detail %q does not contain %q", c.Detail, tc.detailHas)
			}
			if tc.hintAbsent && c.Hint != "" {
				t.Errorf("hint = %q, want none", c.Hint)
			}
			if !strings.Contains(c.Hint, tc.hintHas) {
				t.Errorf("hint %q does not contain %q", c.Hint, tc.hintHas)
			}
			if doctorExitCode(r) != 0 {
				t.Errorf("exit code = %d, want 0: the line is a warning at most", doctorExitCode(r))
			}
		})
	}
}

func TestApprovalLevelCheck(t *testing.T) {
	str := func(v string) *string { return &v }
	cases := []struct {
		name      string
		approvals *api.ApprovalsStatus
		status    string
		detailHas string
	}{
		{"required", &api.ApprovalsStatus{SignInLevelInForce: str("required")}, doctorOK, "needs the approval level"},
		{"auto, sign-in not ok", &api.ApprovalsStatus{
			SignInLevelInForce: str("observe"),
			SignInLevelWarning: str("MEHO does not require the approval level: APPROVAL_SIGN_IN_LEVEL is auto and approvals.sign_in is not_bound, not ok."),
		}, doctorWarn, "approvals.sign_in is not_bound"},
		{"off", &api.ApprovalsStatus{
			SignInLevelInForce: str("off"),
			SignInLevelWarning: str("MEHO does not require the approval level: APPROVAL_SIGN_IN_LEVEL is off."),
		}, doctorWarn, "APPROVAL_SIGN_IN_LEVEL is off"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := &doctorReport{}
			addApprovalLevelCheck(r, &api.HealthResponse{Approvals: tc.approvals})
			c := findCheck(t, r, approvalLevelCheckName)
			if c.Status != tc.status || !strings.Contains(c.Detail, tc.detailHas) {
				t.Errorf("status/detail = %s/%q, want %s/%q", c.Status, c.Detail, tc.status, tc.detailHas)
			}
			if doctorExitCode(r) != 0 {
				t.Errorf("exit code = %d, want 0", doctorExitCode(r))
			}
		})
	}
	r := &doctorReport{}
	addApprovalLevelCheck(r, &api.HealthResponse{Approvals: &api.ApprovalsStatus{}})
	if len(r.Checks) != 0 {
		t.Errorf("an older backplane got a line: %v", r.Checks)
	}
}

// A customer's own Keycloak without the binding: the doctor names the
// operation that fixes it.
func TestDoctor_ReportsAnUnboundApprovalSignIn(t *testing.T) {
	xdg := withTempXDG(t)
	stub := &mcpStub{liveStatus: 200, tools: []string{"meho_status"}, listed: []string{"meho_status"},
		signIn: map[string]any{
			"state": "not_bound", "flow": "meho approval sign-in level", "client": "meho-cli-approve",
			"detail": "the client meho-cli-approve has no own browser flow",
		}}
	srv := httptest.NewServer(stub.handler(t))
	defer srv.Close()
	seedCreds(t, xdg, srv.URL, auth.StoredToken{BackplaneURL: srv.URL, AccessToken: jwtMarker, Expiry: time.Now().Add(time.Hour)})

	r := runAuthed(t, srv.URL, onlineDeps(t))
	c := findCheck(t, r, approvalSignInCheckName)
	if c.Status != doctorWarn || !strings.Contains(c.Hint, "keycloak.authflow.ensure_approval_flow") {
		t.Errorf("unbound approval sign-in not reported with the fix: %+v", c)
	}
}

func TestSecondAdminCheck(t *testing.T) {
	count := func(everyone, platform int) *api.HealthResponse {
		return &api.HealthResponse{Approvals: &api.ApprovalsStatus{
			RightsChangeAdmins: &everyone, RightsChangePlatformAdmins: &platform}}
	}
	hint := "add a platform admin other than the person who asks (in MEHO-Platform-Admins); see " +
		"https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/#changes-of-rights-need-another-admin"
	tail := " Some changes of rights in MEHO's own login realm need a platform admin other than the person who asked."
	for name, tc := range map[string]struct {
		h      *api.HealthResponse
		detail string
	}{
		"one platform admin":              {count(2, 1), "This tenant has only one platform admin." + tail},
		"tenant admins only": {count(3, 0), "This tenant has no platform admin." + tail},
		"no platform admin":               {count(1, 0), "This tenant has no platform admin." + tail},
	} {
		t.Run(name+" warns with the full texts", func(t *testing.T) {
			r := &doctorReport{}
			addSecondAdminCheck(r, tc.h)
			c := findCheck(t, r, secondAdminCheckName)
			if c.Layer != layerAuthed || c.Status != doctorWarn {
				t.Errorf("layer/status = %s/%s, want %s/%s", c.Layer, c.Status, layerAuthed, doctorWarn)
			}
			if c.Detail != tc.detail {
				t.Errorf("detail = %q, want %q", c.Detail, tc.detail)
			}
			if c.Hint != hint {
				t.Errorf("hint = %q, want %q", c.Hint, hint)
			}
			if doctorExitCode(r) != 0 {
				t.Errorf("exit code = %d, want 0: the line is a warning", doctorExitCode(r))
			}
		})
	}
	t.Run("two platform admins is ok", func(t *testing.T) {
		r := &doctorReport{}
		addSecondAdminCheck(r, count(2, 2))
		c := findCheck(t, r, secondAdminCheckName)
		if c.Status != doctorOK || c.Detail != "2 platform admins in this tenant" || c.Hint != "" {
			t.Errorf("status/detail/hint = %s/%q/%q, want ok with 2 platform admins", c.Status, c.Detail, c.Hint)
		}
	})
	two := 2
	for name, h := range map[string]*api.HealthResponse{
		"older backplane":             {},
		"count not known (no number)": {Approvals: &api.ApprovalsStatus{}},
		"only the all-admin count":    {Approvals: &api.ApprovalsStatus{RightsChangeAdmins: &two}},
	} {
		t.Run(name+" gives no line", func(t *testing.T) {
			r := &doctorReport{}
			addSecondAdminCheck(r, h)
			if len(r.Checks) != 0 {
				t.Errorf("got a line: %v", r.Checks)
			}
		})
	}
}

// The fix of the approval flow line says who runs keycloak.authflow.ensure_approval_flow
// and who approves it when it waits.
func TestApprovalFlowFixSaysWhoRunsAndApprovesIt(t *testing.T) {
	want := "a platform admin runs the operation keycloak.authflow.ensure_approval_flow " +
		"on the Keycloak target in the web console; asked with a token, a second platform admin " +
		"approves it; with the built-in Keycloak, `helm upgrade` does it too; see " +
		"https://evoila.github.io/meho/install/keycloak-realm/#add-the-approval-flow-to-a-running-realm-through-meho"
	if approvalFlowFix != want {
		t.Errorf("approvalFlowFix = %q, want %q", approvalFlowFix, want)
	}
}
