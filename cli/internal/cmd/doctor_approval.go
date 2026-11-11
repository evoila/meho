// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"

	"github.com/evoila/meho/cli/internal/api"
)

// approvalSignInCheckName names the doctor line for the approval
// sign-in: the login flow `meho approval sign-in` that `meho approvals
// approve` signs in through, and its binding to the approval client.
const approvalSignInCheckName = "approval sign-in"

// approvalFlowDocs is the page that sets the flow up on a running realm.
const approvalFlowDocs = "https://evoila.github.io/meho/install/keycloak-realm/#add-the-approval-flow-to-a-running-realm-through-meho"

// approvalFlowFix is the fix for a missing flow or binding.
const approvalFlowFix = "a Keycloak admin runs the operation keycloak.authflow.ensure_approval_flow " +
	"on the Keycloak target (it waits for approval); with the built-in Keycloak, `helm upgrade` " +
	"does it too; see " + approvalFlowDocs

// addApprovalSignInCheck adds one line with the state of the approval
// sign-in that the backplane reports in /api/v1/health (approvals.sign_in).
// Only "ok" passes. Every other state is a warning: the doctor checks
// this machine, and the fix is on the server.
func addApprovalSignInCheck(r *doctorReport, h *api.HealthResponse) {
	if h.Approvals == nil || h.Approvals.SignIn == nil {
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			"the backplane does not report the approval sign-in (older version); cannot check", ""))
		return
	}
	s := h.Approvals.SignIn
	flow := "meho approval sign-in"
	if s.Flow != nil && *s.Flow != "" {
		flow = *s.Flow
	}
	detail := ""
	if s.Detail != nil {
		detail = *s.Detail
	}
	switch s.State {
	case api.ApprovalSignInStatusStateOk:
		r.add(authed(approvalSignInCheckName, doctorOK, "",
			fmt.Sprintf("the client %s signs in through the login flow %q", s.Client, flow), ""))
	case api.ApprovalSignInStatusStateFlowMissing:
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			fmt.Sprintf("the realm has no login flow %q; `meho approvals approve` then uses the realm's normal sign-in", flow),
			approvalFlowFix))
	case api.ApprovalSignInStatusStateNotBound:
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			fmt.Sprintf("the login flow %q is not bound: %s", flow, detail), approvalFlowFix))
	case api.ApprovalSignInStatusStateFlowDiffers:
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			fmt.Sprintf("the login flow %q has other steps than MEHO's: %s", flow, detail),
			"a Keycloak admin sets the flow back to the password form and the one-time code step, "+
				"or deletes it; then runs keycloak.authflow.ensure_approval_flow; see "+approvalFlowDocs))
	case api.ApprovalSignInStatusStateNotChecked:
		hint := ""
		if detail == "no_right_to_read_login_flows" {
			hint = "give the backplane's Keycloak admin client the realm-management role view-realm " +
				"(the built-in Keycloak: manage-realm); see " + approvalFlowDocs
		}
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			"the backplane could not check the approval sign-in: "+detail, hint))
	default:
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			fmt.Sprintf("unknown state %q from the backplane: %s", s.State, detail), ""))
	}
}
