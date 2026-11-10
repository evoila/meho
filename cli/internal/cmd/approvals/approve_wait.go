// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// approveWaitHelp is the part of `meho approvals approve --help` that
// explains the background run and --wait (#5110).
const approveWaitHelp = "approve answers at once. MEHO saves the decision " +
	"and runs the operation in the background, so a long operation does " +
	"not hold the command. approve prints \"Approved. The operation is " +
	"running.\" and the command that shows how it goes: meho approvals " +
	"show <id>. With --wait, approve waits until the run has ended and " +
	"then prints how it ended (the Run lines). It asks MEHO every few " +
	"seconds with short calls, so a proxy time limit cannot cut the wait. " +
	"--json prints the request as MEHO has it after the approve (the " +
	"answer of GET /api/v1/approvals/{id}); with --wait, after the run " +
	"has ended. Its result_state field says where the run is: started " +
	"while it runs, then executed, failed or uncertain. When the answer " +
	"to the approve is lost on the network, approve reads the request " +
	"again and prints the state MEHO has."

// lostAnswerNote is printed when the answer to the decision was lost and
// the command read the request again.
const lostAnswerNote = "The answer to this call was lost on the network. " +
	"This is the state MEHO has now:"

// runEndedNoResultNote is printed by --wait when the background run
// ended without a stored result (for example after a server restart).
const runEndedNoResultNote = "The run ended, but MEHO stored no result. " +
	uncertainAdvice

// stillWaitingNote is printed once by --wait when MEHO lists the request
// as "outcome unknown" while the CLI cannot check the run itself.
const stillWaitingNote = "The run takes longer than usual and MEHO has no " +
	"result yet. Still waiting. Press Ctrl-C to stop; the run goes on."

// waitPollInterval is how long --wait waits between two reads. A
// variable so tests can make it short.
var waitPollInterval = 3 * time.Second

// maxWaitFailures is how many reads in a row may fail before --wait
// gives up and prints the error.
const maxWaitFailures = 5

// decisionOpts are the flags of approve and reject that runDecision reads.
type decisionOpts struct {
	Wait              bool
	JSONOut           bool
	BackplaneOverride string
}

// decideAnswer is the answer of POST /decide: done when MEHO ran the
// operation before it answered (HTTP 200, also every rejection),
// accepted when the operation runs in the background (HTTP 202).
type decideAnswer struct {
	done     *api.DecideResponseBody
	accepted *api.DecideAcceptedBody
}

// decided is the answer in the shape the self-approval line reads. nil
// when there is no answer (it was lost).
func (a *decideAnswer) decided() *api.DecideResponseBody {
	if a == nil {
		return nil
	}
	if a.done != nil {
		return a.done
	}
	if a.accepted != nil {
		return &api.DecideResponseBody{
			ApprovalRequestId: a.accepted.ApprovalRequestId,
			Decision:          a.accepted.Decision,
			Reason:            a.accepted.Reason,
			SelfApproved:      a.accepted.SelfApproved,
			ApprovalRule:      a.accepted.ApprovalRule,
		}
	}
	return nil
}

// runID is the id of the background run, or nil.
func (a *decideAnswer) runID() *uuid.UUID {
	if a == nil || a.accepted == nil {
		return nil
	}
	id := a.accepted.RunId
	return &id
}

// isLostAnswer reports whether err is a network failure of the call, so
// the call may have reached MEHO and only the answer is missing. An
// HTTP answer from MEHO (any status) is not a lost answer.
func isLostAnswer(err error) bool {
	if err == nil || errors.Is(err, context.Canceled) {
		return false
	}
	var he *httpResponseError
	if errors.As(err, &he) {
		return false
	}
	var ue *url.Error
	return errors.As(err, &ue) || errors.Is(err, io.ErrUnexpectedEOF)
}

// refusedAsyncField reports whether MEHO refused the call because it does
// not know the "async" field (a server from before #5110). Such a call
// saved nothing, so sending it again without the field is safe.
func refusedAsyncField(err error) bool {
	var he *httpResponseError
	if !errors.As(err, &he) || he.statusCode != http.StatusUnprocessableEntity {
		return false
	}
	body := string(he.body)
	return strings.Contains(body, `"async"`) && strings.Contains(body, "extra")
}

// runHasResult reports whether the request is not approved or its run
// has a stored result.
func runHasResult(d *api.ApprovalRequestView) bool {
	if d.ResultState == nil {
		return true
	}
	switch *d.ResultState {
	case api.ApprovalResultStateExecuted, api.ApprovalResultStateFailed, api.ApprovalResultStateUncertain:
		return true
	}
	return false
}

// runHasEnded reports whether the request needs no "running" hint: its
// run has a result, or MEHO lists it as "outcome unknown".
func runHasEnded(d *api.ApprovalRequestView) bool {
	return runHasResult(d) || d.OutcomeUnknown != nil
}

// printRunHint tells the person where the run is when it has not ended.
func printRunHint(w io.Writer, idArg string, d *api.ApprovalRequestView, endedWithoutResult bool) {
	if endedWithoutResult {
		fmt.Fprintln(w, runEndedNoResultNote)
		return
	}
	if !runHasEnded(d) {
		fmt.Fprintf(w, "Approved. The operation is running. Check it with `meho approvals show %s`.\n",
			output.SafeText(idArg))
	}
}

// waitForRun reads the request every waitPollInterval until its run has
// ended. Each read is a short call, so no proxy time limit applies. A
// failed read is tried again, up to maxWaitFailures in a row.
//
// When runID is set and this person may read runs, the run itself is
// checked too: a run that ended without storing a result on the request
// (a crash, a server restart) ends the wait, and the bool says so.
// The "outcome unknown" list (a run that takes longer than that age
// limit) never ends the wait. Without the run check, the wait goes on
// until the request has a result, and prints stillWaitingNote once.
func waitForRun(
	cmd *cobra.Command,
	client *api.AuthedClient,
	requestID uuid.UUID,
	runID *uuid.UUID,
	jsonOut bool,
) (*api.ApprovalRequestView, bool, error) {
	ctx := cmd.Context()
	if !jsonOut {
		fmt.Fprintln(cmd.ErrOrStderr(), "Approved. The operation is running. Waiting until it ends ...")
	}
	failures := 0
	noted := false
	for {
		select {
		case <-ctx.Done():
			return nil, false, ctx.Err()
		case <-time.After(waitPollInterval):
		}
		again, err := fetchDetail(ctx, client, requestID)
		if err != nil {
			failures++
			if failures >= maxWaitFailures {
				return nil, false, err
			}
			continue
		}
		failures = 0
		detail := again
		if runHasResult(detail) {
			return detail, false, nil
		}
		if runID != nil && runEndedElsewhere(ctx, client, *runID, &runID) {
			// The run ended; read the request once more, in case it
			// stored its result just before the run did.
			if last, err := fetchDetail(ctx, client, requestID); err == nil {
				detail = last
			}
			return detail, !runHasResult(detail), nil
		}
		if runID == nil && detail.OutcomeUnknown != nil && !noted && !jsonOut {
			fmt.Fprintln(cmd.ErrOrStderr(), stillWaitingNote)
			noted = true
		}
	}
}

// runEndedElsewhere reports whether the background run has ended. When
// this person may not read runs (or the read fails for good), or when the
// run only found that another caller runs the operation
// ("already_resumed"), it clears *runID so the wait goes on with the
// request alone.
func runEndedElsewhere(ctx context.Context, client *api.AuthedClient, runID uuid.UUID, keep **uuid.UUID) bool {
	resp, err := client.GetOperationRunStatusApiV1OperationsRunsHandleGetWithResponse(
		ctx, runID, &api.GetOperationRunStatusApiV1OperationsRunsHandleGetParams{})
	if err != nil {
		return false
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		*keep = nil
		return false
	}
	run := resp.JSON200
	if run.Status == api.OperationRunStatusSucceeded && run.Result != nil &&
		(*run.Result)["status"] == "already_resumed" {
		// Another caller (usually the waiting requester) won the run, so
		// this run says nothing about the operation. Wait on the request.
		*keep = nil
		return false
	}
	switch run.Status {
	case api.OperationRunStatusSucceeded, api.OperationRunStatusFailed,
		api.OperationRunStatusCancelled, api.OperationRunStatusUncertain:
		return true
	}
	return false
}
