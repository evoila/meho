// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package cmd

import (
	"fmt"

	"github.com/evoila/meho/cli/internal/api"
)

// approvalSignInCheckName names the doctor line for the approval
// sign-in: the login flow `meho approval sign-in level` that `meho approvals
// approve` signs in through, and its binding to the approval client.
const approvalSignInCheckName = "approval sign-in"

// approvalFlowDocs is the page that sets the flow up on a running realm.
const approvalFlowDocs = "https://evoila.github.io/meho/install/keycloak-realm/#add-the-approval-flow-to-a-running-realm-through-meho"

// approvalFlowFix is the fix for a missing flow or binding.
const approvalFlowFix = "a Keycloak admin runs the operation keycloak.authflow.ensure_approval_flow " +
	"on the Keycloak target (it waits for approval); with the built-in Keycloak, `helm upgrade` " +
	"does it too; see " + approvalFlowDocs

// approvalLevelCheckName names the doctor line for the approval level.
const approvalLevelCheckName = "approval level"

// addApprovalLevelCheck adds one line that says whether MEHO requires the
// approval level of an approval sign-in now (approvals.sign_in_level_in_force
// in /api/v1/health, #6281). Only "required" passes. A backplane that
// does not report it gets no line.
func addApprovalLevelCheck(r *doctorReport, h *api.HealthResponse) {
	if h.Approvals == nil || h.Approvals.SignInLevelInForce == nil {
		return
	}
	a := h.Approvals
	if *a.SignInLevelInForce == "required" && a.SignInLevelWarning == nil {
		r.add(authed(approvalLevelCheckName, doctorOK, "",
			"an approve on the command line needs the approval level", ""))
		return
	}
	warning := "MEHO does not require the approval level"
	if a.SignInLevelWarning != nil {
		warning = *a.SignInLevelWarning
	}
	r.add(authed(approvalLevelCheckName, doctorWarn, "", warning,
		"fix the approval sign-in line above, or set APPROVAL_SIGN_IN_LEVEL (chart config.approvalSignInLevel) "+
			"to auto or required; see "+approvalFlowDocs))
}

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
	flow := "meho approval sign-in level"
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
			fmt.Sprintf("the login flow %q or the approval level differs from MEHO's: %s", flow, detail),
			"a Keycloak admin fixes what the line names: the flow's steps (the level step, the password form, "+
				"the one-time code step), the level step's settings (level 2, maximum age 0), or the sign-in "+
				"levels (acr.loa.map: meho-approve is level 2, no other name has level 2, the client has no own map); "+
				"then runs keycloak.authflow.ensure_approval_flow; see "+approvalFlowDocs))
	case api.ApprovalSignInStatusStateOtherGrantOn:
		r.add(authed(approvalSignInCheckName, doctorWarn, "",
			"the approval client also allows a sign-in that skips the login flow: "+detail,
			"a Keycloak admin turns off direct grant and device grant on the client "+s.Client+"; see "+approvalFlowDocs))
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
