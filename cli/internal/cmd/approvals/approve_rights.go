// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"errors"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// approveRightsHelp is the part of `meho approvals approve --help` that
// explains who may approve (#3329). The server decides; the CLI only
// shows its answer.
const approveRightsHelp = "Who may approve: a MEHO-SA-Operator may approve their " +
	"own change. Then approve prints a self-approval line, and the audit " +
	"log records the self-approval. Another person's dangerous or " +
	"destructive change needs a MEHO-SA-Operator or a MEHO-Admin. MEHO " +
	"refuses an approval you may not make (403); approve then prints " +
	"MEHO's reason as it is. meho approvals list and show say for each " +
	"request whether you may approve it. " + approveWebHint

// approveWebHint ends every approve refusal (403) and the help text.
const approveWebHint = "The web interface is the normal place to approve: " +
	"it shows for each request whether you may approve it."

// Reasons the server sends in may_approve_reason (#3329).
const (
	reasonOK               = "ok"
	reasonOwnChange        = "own_change"
	reasonWrongGroup       = "wrong_group"
	reasonRoleTooLow       = "role_too_low"
	reasonNoApprovalAccess = "no_approval_access"
	reasonNotAPerson       = "not_a_person"
	reasonNotPending       = "not_pending"
)

// Approval rules the server sends in approval_rule after an approve.
const (
	ruleSAOperatorSelf = "sa_operator_self_approval"
	ruleBreakGlassSelf = "break_glass_self_approval"
)

// selfApprovalLine returns the line approve prints after a
// self-approval, or "" for any other decision. It reads the rule the
// server used (approval_rule on the /decide answer).
func selfApprovalLine(resp *api.DecideResponseBody) string {
	if resp == nil || resp.SelfApproved == nil || !*resp.SelfApproved || resp.ApprovalRule == nil {
		return ""
	}
	switch *resp.ApprovalRule {
	case ruleSAOperatorSelf:
		return "self-approved as a MEHO-SA-Operator: this is your own change, recorded in the audit log"
	case ruleBreakGlassSelf:
		return "self-approved because APPROVAL_ALLOW_SELF_APPROVAL is on: this is your own change, recorded in the audit log"
	default:
		return "self-approved: this is your own change, recorded in the audit log"
	}
}

// routeApproveError is routeRequestError for an approve. A 403 prints
// the server's text as it is, followed by approveWebHint.
func routeApproveError(cmd *cobra.Command, backplaneURL string, err error, jsonOut bool) error {
	var he *httpResponseError
	if errors.As(err, &he) && he.statusCode == http.StatusForbidden {
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(trimmedBody(he.body)+" "+approveWebHint),
			jsonOut,
		)
	}
	return routeRequestError(cmd, backplaneURL, err, jsonOut)
}

// approveRightLabel is the one-word APPROVE value of the list table:
// "yes", "yes:own_change", "no:<reason>", or "-" for a request that is
// not pending or a server that sent no answer.
func approveRightLabel(may *bool, reason *string) string {
	if may == nil || reason == nil || *reason == reasonNotPending {
		return "-"
	}
	if *may {
		if *reason == reasonOK {
			return "yes"
		}
		return "yes:" + *reason
	}
	return "no:" + *reason
}

// approveRightText is the "May approve" line of the show view: yes or
// no, with the server's reason in plain words. "" when the server sent
// no answer.
func approveRightText(may *bool, reason *string) string {
	if may == nil || reason == nil {
		return ""
	}
	if *may {
		if *reason == reasonOwnChange {
			return "yes, your own change (a self-approval, recorded in the audit log)"
		}
		return "yes"
	}
	switch *reason {
	case reasonOwnChange:
		return "no, it is your own change"
	case reasonWrongGroup:
		return "no, another person's dangerous or destructive change needs a MEHO-SA-Operator or a MEHO-Admin"
	case reasonRoleTooLow:
		return "no, the operation needs a higher role than yours"
	case reasonNoApprovalAccess:
		return "no, your role cannot decide approval requests"
	case reasonNotAPerson:
		return "no, only a person can approve"
	case reasonNotPending:
		return "no, the request is not pending"
	default:
		return "no (" + *reason + ")"
	}
}
