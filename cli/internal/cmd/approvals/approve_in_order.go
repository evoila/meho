// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// inOrderHelp is the part of `meho approvals approve --help` that
// explains --filter-work-ref REF --in-order (#3597).
const inOrderHelp = "With --filter-work-ref REF --in-order, approve takes every " +
	"request of change ticket REF, oldest first, and approves the " +
	"pending ones one by one. Each step is an ordinary single " +
	"approval: it runs the operation at once and has its own audit " +
	"row and its own checks. After each step it reads how the run " +
	"ended. It goes on only when the run was OK. When the waiting " +
	"requester runs the step instead, it waits for that run's stored " +
	"result (up to two minutes) and goes on only when it was OK. It " +
	"stops at the first run that failed or was denied, at a run whose " +
	"result is not known in time, at a step that is already rejected, " +
	"expired, withdrawn or approved with a failed or unfinished run, " +
	"and at any refusal (for example you sent the request yourself). " +
	"Then it exits non-zero and names the step that stopped it and the " +
	"steps it did not run.\n\n" +
	"At each pending step it shows what the step does (the operation, " +
	"the connector and its version, the target, the risk level and the " +
	"parameters) and asks you about it. Type yes to approve that one " +
	"step and go on; anything else stops the run. One sign-in covers " +
	"the steps; a destructive step gets a sign-in of its own.\n\n" +
	"Run it again after a stop: steps that were approved and ran OK " +
	"are skipped. A step that was approved but whose run failed " +
	"cannot run again from here; ask the requester to send it again. " +
	"The run is not one transaction: if you stop the command, the " +
	"steps it already approved stay approved."

// How long the in-order run waits for the stored result of a step the
// waiting requester runs (dispatch_status "already_resumed"), and how
// often it reads the request. Tests shorten both.
var (
	inOrderResultWait = 2 * time.Minute
	inOrderResultPoll = time.Second
)

const stepNotEnded = "its run has not ended yet, or no result was stored. Check it with " +
	"meho approvals show %s and run this again when it has ended"

// Outcomes of one step of an in-order run.
const (
	stepSkipped  = "skipped"  // approved earlier and ran OK
	stepApproved = "approved" // approved by this run, and the run was OK
	stepStopped  = "stopped"  // the run stopped at this step
	stepNotRun   = "not_run"  // after the stop; still as it was
)

type inOrderOpts struct {
	WorkRef           string
	Reason            string
	JSONOut           bool
	BackplaneOverride string
}

// inOrderStep is one request of the change ticket and what the run did
// with it.
type inOrderStep struct {
	Step        int    `json:"step"`
	ID          string `json:"id"`
	OpID        string `json:"op_id"`
	Status      string `json:"status"`
	SafetyLevel string `json:"safety_level,omitempty"`
	Outcome     string `json:"outcome"`
	Detail      string `json:"detail,omitempty"`
}

// inOrderResult is the --json output of an in-order run.
type inOrderResult struct {
	WorkRef   string        `json:"work_ref"`
	Completed bool          `json:"completed"`
	StoppedAt int           `json:"stopped_at,omitempty"`
	Warnings  []string      `json:"warnings,omitempty"`
	Steps     []inOrderStep `json:"steps"`
}

// stepStop says why the run stopped at a step. err is set when the
// server refused the call (HTTP error) or could not be reached; the
// command then returns the same error the single-id approve returns.
type stepStop struct {
	detail string
	err    error
}

// approveArgs checks the positional args against --filter-work-ref/--in-order:
// one or more ids for an approve, none for an in-order run.
func approveArgs(workRef *string, inOrder *bool) cobra.PositionalArgs {
	return func(_ *cobra.Command, args []string) error {
		if *inOrder {
			if strings.TrimSpace(*workRef) == "" {
				return errors.New("--in-order needs --filter-work-ref <change ticket>")
			}
			if len(args) != 0 {
				return errors.New("--in-order takes no request id; it approves the requests of --filter-work-ref")
			}
			return nil
		}
		if *workRef != "" {
			return errors.New("--filter-work-ref needs --in-order; to approve requests, pass their ids")
		}
		if len(args) == 0 {
			return errors.New("approve takes one or more request ids")
		}
		return nil
	}
}

// runInOrder approves the pending requests of one change ticket, oldest
// first, and stops at the first step that does not end OK (#3597).
func runInOrder(cmd *cobra.Command, opts inOrderOpts) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	items, err := fetchList(cmd.Context(), client, listOpts{
		StatusFilter: "all",
		WorkRef:      opts.WorkRef,
		Order:        "oldest",
	})
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if len(items) == 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("no approval requests for change ticket %q", opts.WorkRef)),
			opts.JSONOut,
		)
	}

	res := inOrderResult{WorkRef: opts.WorkRef, Warnings: groupWarnings(items)}
	for _, w := range res.Warnings {
		fmt.Fprintf(cmd.ErrOrStderr(), "warning: %s\n", w)
	}
	r := &inOrderRunner{
		cmd: cmd, client: client, opts: opts, in: bufio.NewReader(cmd.InOrStdin()),
		total: len(items), signIn: newApprovalSignIn(client),
	}
	var stop *stepStop
	for i := range items {
		step := newInOrderStep(i+1, &items[i])
		if stop != nil {
			step.Outcome = stepNotRun
			res.Steps = append(res.Steps, step)
			continue
		}
		stop = r.runStep(cmd.Context(), &items[i], &step)
		if stop != nil {
			step.Outcome = stepStopped
			step.Detail = stop.detail
			res.StoppedAt = step.Step
		}
		res.Steps = append(res.Steps, step)
		if !opts.JSONOut {
			printInOrderStep(cmd.OutOrStdout(), step, len(items))
		}
	}
	res.Completed = stop == nil

	if opts.JSONOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), res); err != nil {
			return err
		}
	} else {
		printInOrderSummary(cmd.OutOrStdout(), res)
	}
	if stop == nil {
		return nil
	}
	if stop.err != nil {
		return routeRequestError(cmd, backplaneURL, stop.err, opts.JSONOut)
	}
	return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(stopMessage(res)), opts.JSONOut)
}

type inOrderRunner struct {
	cmd    *cobra.Command
	client *api.AuthedClient
	opts   inOrderOpts
	in     *bufio.Reader
	total  int
	// signIn is the one approval sign-in of the run (#5515).
	signIn *approvalSignIn
}

// runStep handles one request. It returns nil when the run may go on to
// the next step, or why it must stop here.
func (r *inOrderRunner) runStep(ctx context.Context, item *api.ApprovalRequestView, step *inOrderStep) *stepStop {
	switch item.Status {
	case api.ApprovalRequestStatusApproved:
		if item.ResultState != nil && *item.ResultState == api.ApprovalResultStateExecuted {
			step.Outcome = stepSkipped
			step.Detail = "approved earlier and ran OK"
			return nil
		}
		if item.ResultState != nil && *item.ResultState == api.ApprovalResultStateStarted {
			return &stepStop{detail: "approved earlier; " + fmt.Sprintf(stepNotEnded, step.ID)}
		}
		return &stepStop{detail: fmt.Sprintf(
			"approved earlier, but its run did not end OK (run: %s). An approval is used once, "+
				"so this step cannot run again from here. Ask the requester to send it again",
			runStateLabel(item.ResultState))}
	case api.ApprovalRequestStatusPending:
	default:
		return &stepStop{detail: fmt.Sprintf(
			"this step is %s. The steps after it may need it, so the run stops here", item.Status)}
	}

	ok, err := r.confirm(ctx, item, step)
	if err != nil {
		return &stepStop{detail: "could not read the step before asking for a yes", err: err}
	}
	if !ok {
		return &stepStop{detail: "you did not type yes for this step"}
	}

	body := api.DecideRequestBody{Decision: "approved"}
	if r.opts.Reason != "" {
		reason := r.opts.Reason
		body.Reason = &reason
	}
	resp, err := sendDecide(ctx, r.client, item.Id, body, r.signIn, step.SafetyLevel == "destructive")
	if err != nil {
		return &stepStop{detail: "the approval was refused: " + err.Error(), err: err}
	}
	stop, claimedElsewhere := checkDispatch(resp, step)
	if claimedElsewhere {
		return r.waitForStoredResult(ctx, item.Id, step)
	}
	return stop
}

// checkDispatch reads how the run of a just-approved step ended. Only
// "ok" lets the run go on. "already_resumed" means the waiting
// requester claimed the run, not that it ended; the second return value
// is then true and the caller waits for the stored result. Any other
// answer, including an answer that is not a finished 200, stops the run.
func checkDispatch(resp *api.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostResponse, step *inOrderStep) (*stepStop, bool) {
	if resp.StatusCode() != 200 || resp.JSON200 == nil {
		return &stepStop{detail: fmt.Sprintf(
			"approved, but MEHO answered HTTP %d without a finished run, so the run cannot tell "+
				"whether the step ran. Check it with meho approvals show %s", resp.StatusCode(), step.ID)}, false
	}
	status := ""
	if resp.JSON200.DispatchStatus != nil {
		status = *resp.JSON200.DispatchStatus
	}
	switch status {
	case "ok":
		step.Outcome = stepApproved
		step.Detail = "ran OK"
		return nil, false
	case "already_resumed":
		return nil, true
	}
	detail := fmt.Sprintf("approved, but the run ended with %q", status)
	if resp.JSON200.DispatchError != nil && *resp.JSON200.DispatchError != "" {
		detail += ": " + *resp.JSON200.DispatchError
	}
	return &stepStop{detail: detail + ". An approval is used once, so this step cannot run again " +
		"from here. Ask the requester to send it again"}, false
}

// waitForStoredResult reads the request until the run the waiting
// requester claimed has a stored result. "executed" lets the run go on;
// "failed" stops it. No result within inOrderResultWait also stops it:
// the outcome is unknown, and the steps after it may depend on it.
func (r *inOrderRunner) waitForStoredResult(ctx context.Context, id uuid.UUID, step *inOrderStep) *stepStop {
	deadline := time.Now().Add(inOrderResultWait)
	for {
		detail, err := fetchDetail(ctx, r.client, id)
		if err != nil {
			return &stepStop{detail: "approved; the waiting requester is running it, but MEHO could not " +
				"read how the run ended", err: err}
		}
		if detail != nil && detail.ResultState != nil {
			switch *detail.ResultState {
			case api.ApprovalResultStateExecuted:
				step.Outcome = stepApproved
				step.Detail = "the waiting requester ran it; ran OK"
				return nil
			case api.ApprovalResultStateFailed:
				return &stepStop{detail: "approved, but the run by the waiting requester failed. An approval " +
					"is used once, so this step cannot run again from here. Ask the requester to send it again"}
			}
		}
		if time.Now().After(deadline) {
			return &stepStop{detail: "approved; the waiting requester is running it, but " +
				fmt.Sprintf(stepNotEnded, step.ID)}
		}
		timer := time.NewTimer(inOrderResultPoll)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return &stepStop{detail: "approved; the waiting requester is running it, but the wait for " +
				"its result was cancelled. Check it with meho approvals show " + step.ID, err: ctx.Err()}
		}
	}
}

// confirm shows a pending step and asks for a yes on stdin. The text
// goes to stderr so --json output on stdout stays clean.
func (r *inOrderRunner) confirm(ctx context.Context, item *api.ApprovalRequestView, step *inOrderStep) (bool, error) {
	detail, err := fetchDetail(ctx, r.client, item.Id)
	if err != nil {
		return false, err
	}
	if detail == nil {
		detail = item
	}
	w := r.cmd.ErrOrStderr()
	level := step.SafetyLevel
	if level == "" {
		level = "of unknown risk level"
	}
	showRequest(w, fmt.Sprintf("Step %d of %d is %s. Approving it runs it at once.", step.Step, r.total, level), detail)
	return askYes(r.in, w, fmt.Sprintf("Type yes to approve step %d and go on. Anything else stops here: ", step.Step)), nil
}

func newInOrderStep(n int, item *api.ApprovalRequestView) inOrderStep {
	step := inOrderStep{
		Step:   n,
		ID:     item.Id.String(),
		OpID:   item.OpId,
		Status: string(item.Status),
	}
	if level, ok := item.ProposedEffect["safety_level"].(string); ok {
		step.SafetyLevel = level
	}
	return step
}

// groupWarnings checks the two conditions under which oldest first may
// not be the order the steps depend on: requests from more than one
// requester, and two requests with no clear time order.
func groupWarnings(items []api.ApprovalRequestView) []string {
	var warnings []string
	subs := map[string]bool{}
	for _, it := range items {
		subs[it.PrincipalSub] = true
	}
	if len(subs) > 1 {
		names := make([]string, 0, len(subs))
		for s := range subs {
			names = append(names, s)
		}
		sort.Strings(names)
		warnings = append(warnings, fmt.Sprintf(
			"this change ticket has requests from %d requesters (%s). Oldest first is the right "+
				"order only for one chain sent by one requester", len(names), strings.Join(names, ", ")))
	}
	for i := 1; i < len(items); i++ {
		prev, perr := time.Parse(time.RFC3339Nano, items[i-1].CreatedAt)
		cur, cerr := time.Parse(time.RFC3339Nano, items[i].CreatedAt)
		if perr != nil || cerr != nil {
			continue
		}
		if !prev.Before(cur) {
			warnings = append(warnings, fmt.Sprintf(
				"steps %d and %d were created at the same time or out of order, so MEHO cannot tell "+
					"which comes first", i, i+1))
		}
	}
	return warnings
}

func printInOrderStep(w io.Writer, step inOrderStep, total int) {
	level := step.SafetyLevel
	if level == "" {
		level = "-"
	}
	fmt.Fprintf(w, "step %d/%d  %-8s %-11s %s  %s", step.Step, total, step.Outcome, level, step.OpID, step.ID)
	if step.Detail != "" {
		fmt.Fprintf(w, "  (%s)", step.Detail)
	}
	fmt.Fprintln(w)
}

func printInOrderSummary(w io.Writer, res inOrderResult) {
	if res.Completed {
		approved := 0
		for _, s := range res.Steps {
			if s.Outcome == stepApproved {
				approved++
			}
		}
		if approved == 0 {
			fmt.Fprintf(w, "\nNothing to approve: every step of %s already ran OK.\n", res.WorkRef)
			return
		}
		fmt.Fprintf(w, "\nDone: approved %d step(s) of %s in order.\n", approved, res.WorkRef)
		return
	}
	for _, s := range res.Steps {
		if s.Outcome == stepNotRun {
			printInOrderStep(w, s, len(res.Steps))
		}
	}
	fmt.Fprintf(w, "\n%s\n", stopMessage(res))
}

// stopMessage names the step the run stopped at and the steps it did
// not run.
func stopMessage(res inOrderResult) string {
	var stopped inOrderStep
	var notRun []string
	for _, s := range res.Steps {
		switch s.Outcome {
		case stepStopped:
			stopped = s
		case stepNotRun:
			notRun = append(notRun, fmt.Sprintf("%d (%s)", s.Step, s.ID))
		}
	}
	msg := fmt.Sprintf("stopped at step %d of %d (%s %s): %s.",
		stopped.Step, len(res.Steps), stopped.OpID, stopped.ID, stopped.Detail)
	if len(notRun) == 0 {
		return msg + " No steps were left after it."
	}
	return msg + " Not run: steps " + strings.Join(notRun, ", ") + "."
}
