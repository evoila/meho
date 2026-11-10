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
const revealNotice = "Hidden values: the approved run returns the secret values to the requester."

// dispatchWaitingForRequester is the dispatch_status of an approve of a read
// that shows secret values. Nothing ran: the person who asked gets the value
// later, once, and only for a short time after the approval.
const dispatchWaitingForRequester = "waiting_for_requester"

// waitsForRequester reports whether the /decide answer d says the approved
// read runs only when the person who asked gets the value.
func waitsForRequester(d *api.DecideResponseBody) bool {
	return d != nil && d.DispatchStatus != nil && *d.DispatchStatus == dispatchWaitingForRequester
}

// viewWaitsForRequester reports whether d is an approved read that shows
// secret values and has not run: it runs only when the person who asked
// gets the value.
func viewWaitsForRequester(d *api.ApprovalRequestView) bool {
	return d.Status == api.ApprovalRequestStatusApproved &&
		d.WaitsForRequester != nil && *d.WaitsForRequester &&
		d.ResultState != nil && *d.ResultState == api.ApprovalResultStateNotStarted
}

// requesterFetchNote says what happens after the approve of such a read.
func requesterFetchNote(id string) string {
	return fmt.Sprintf("Approved. The read did not run. Only the person who asked can get the value, "+
		"once, within the time limit (15 minutes by default): `meho secret read --approval %s`, or "+
		"the \"Show value\" button in the console. After the time limit, they must ask again.",
		output.SafeText(id))
}

// printRevealNotice writes revealNotice, after indent, when the approved run
// of d returns the hidden values to the requester. It writes nothing
// otherwise.
func printRevealNotice(w io.Writer, indent string, d *api.ApprovalRequestView) {
	if d.RevealSecret != nil && *d.RevealSecret {
		fmt.Fprintf(w, "%s%s\n", indent, revealNotice)
	}
}
