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
			`signs in through the login flow "meho approval sign-in"`, "", true},
		{"flow missing", signInHealth(api.ApprovalSignInStatusStateFlowMissing, ""), doctorWarn,
			`no login flow "meho approval sign-in"`, "keycloak.authflow.ensure_approval_flow", false},
		{"not bound", signInHealth(api.ApprovalSignInStatusStateNotBound, "the client meho-cli-approve has no own browser flow"), doctorWarn,
			"has no own browser flow", "keycloak.authflow.ensure_approval_flow", false},
		{"flow differs", signInHealth(api.ApprovalSignInStatusStateFlowDiffers, "the flow has 5 steps; wanted 4"), doctorWarn,
			"other steps than MEHO's: the flow has 5 steps", "sets the flow back", false},
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

// A customer's own Keycloak without the binding: the doctor names the
// operation that fixes it.
func TestDoctor_ReportsAnUnboundApprovalSignIn(t *testing.T) {
	xdg := withTempXDG(t)
	stub := &mcpStub{liveStatus: 200, tools: []string{"meho_status"}, listed: []string{"meho_status"},
		signIn: map[string]any{
			"state": "not_bound", "flow": "meho approval sign-in", "client": "meho-cli-approve",
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
