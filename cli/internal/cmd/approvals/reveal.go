// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"fmt"
	"io"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// revealNotice is the line every approval view prints when the requester
// asked for the hidden values of the answer (reveal_secret: true).
const revealNotice = "Hidden values: approving runs nothing. It runs once, when the requester gets the secret values."

// dispatchWaitingForRequester is the dispatch_status of an approve of a
// request that shows secret values (a read, or an operation that creates a
// credential). Nothing ran: the person who asked gets the value later, once,
// and only for a short time after the approval.
const dispatchWaitingForRequester = "waiting_for_requester"

// waitsForRequester reports whether the /decide answer d says the approved
// request runs only when the person who asked gets the value.
func waitsForRequester(d *api.DecideResponseBody) bool {
	return d != nil && d.DispatchStatus != nil && *d.DispatchStatus == dispatchWaitingForRequester
}

// viewWaitsForRequester reports whether d is an approved request that shows
// secret values and has not run: it runs only when the person who asked
// gets the value.
func viewWaitsForRequester(d *api.ApprovalRequestView) bool {
	return d.Status == api.ApprovalRequestStatusApproved &&
		d.WaitsForRequester != nil && *d.WaitsForRequester &&
		d.ResultState != nil && *d.ResultState == api.ApprovalResultStateNotStarted
}

// requesterFetchNote says what happens after the approve of such a request.
func requesterFetchNote(id string) string {
	safe := output.SafeText(id)
	return fmt.Sprintf("Approved. The operation did not run. It runs once, when the person who asked "+
		"gets the value, within the time limit (15 minutes by default): `meho secret read --approval %s`, "+
		"POST /api/v1/approvals/%s/reveal, or the \"Show value\" button in the console. After the "+
		"time limit, they must ask again.", safe, safe)
}

// printRevealNotice writes revealNotice, after indent, when the requester of
// d asked for the hidden values of the answer. It writes nothing
// otherwise.
func printRevealNotice(w io.Writer, indent string, d *api.ApprovalRequestView) {
	if d.RevealSecret != nil && *d.RevealSecret {
		fmt.Fprintf(w, "%s%s\n", indent, revealNotice)
	}
}
