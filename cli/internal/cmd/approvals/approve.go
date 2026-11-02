// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/cmd/workref"
	"github.com/evoila/meho/cli/internal/output"
)

// delegatedReasonHelp explains the layout the server's parse_delegation
// accepts for a reason that records who told the operator to decide.
// Shared by approve and reject so both help texts stay the same; verb
// is "approve" or "reject" so each example names its own command.
func delegatedReasonHelp(verb string) string {
	return "If another person told you in writing to " +
		"make this decision, end the reason with these three lines:\n\n" +
		"  Instructed-by: <who>\n" +
		"  Instruction: \"<their words>\"\n" +
		"  Refs: <issue or change ref>\n\n" +
		"Free text may come first. Then an empty line must separate it " +
		"from the three lines. Each of the three lines must be a single " +
		"Key: value line; a value cannot go on to the next line. Use " +
		"real line breaks inside the quotes, not the two characters \\n. " +
		"A reason with Instructed-by: in the wrong layout, or without " +
		"the other two lines, is refused (422) and the CLI prints the " +
		"server's message. Example:\n\n" +
		"  meho approvals " + verb + " <id> --reason \"Ran the cleanup in the change window.\n\n" +
		"  Instructed-by: lead@example.com\n" +
		"  Instruction: \\\"please run the cleanup tonight\\\"\n" +
		"  Refs: #12\""
}

// approveSignInHelp is the part of `meho approvals approve --help` that
// explains the approval sign-in (#3979, #5515).
const approveSignInHelp = "An approval needs proof that you are present: a fresh " +
	"sign-in in the browser, made only for approvals. Your `meho login` " +
	"token never approves. So approve opens your browser and asks you to " +
	"sign in again. One sign-in covers approvals for 15 minutes (the " +
	"server can set another time), so one run can approve several " +
	"requests with one sign-in. A destructive operation needs a sign-in " +
	"of its own: approve opens the browser again for it. The CLI keeps " +
	"the sign-in in memory for this one run only. It never stores it. " +
	"Without a browser it prints the sign-in link and stops; approve in " +
	"the web interface instead. Reject needs no new sign-in."

// severalIDsHelp explains `meho approvals approve <id> <id> ...`.
const severalIDsHelp = "With several ids, approve shows each request (what it " +
	"does, the operation, the connector and its version, the target, the " +
	"risk level and the parameters) and asks you about each one on its " +
	"own. Type yes to approve that request; anything else skips it. Each " +
	"approval is a separate decision about one request."

// newApproveCmd returns the `meho approvals approve` command.
//
//	meho approvals approve <id> [<id>...] [--reason TEXT] [--json] [--backplane <url>]
//	meho approvals approve --filter-work-ref REF --in-order [--reason TEXT] [--json] [--backplane <url>]
//
// Role: operator. Approves a pending approval request via
// POST /api/v1/approvals/{id}/decide. The backplane records the
// decision durably and broadcasts approval_decided; the paused agent
// run resumes off the broadcast (#1117 path). With several ids it shows
// each request and asks for a yes for each one (#5515). With
// --filter-work-ref and --in-order it approves every pending request of
// one change ticket, oldest first, and stops at the first step that
// fails (#3597).
func newApproveCmd() *cobra.Command {
	var (
		reason            string
		workRef           string
		inOrder           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "approve <id> [<id>...] | --filter-work-ref REF --in-order",
		Short: "Approve pending approval requests",
		Long: "approve calls POST /api/v1/approvals/{id}/decide with " +
			"decision=approved to flip the request status, write the " +
			"decision audit row, and broadcast approval_decided. The " +
			"paused agent run resumes off the broadcast (T9 #1117). " +
			"Only pending requests can be approved; any other status " +
			"returns 409. --reason attaches a human-readable " +
			"rationale to the decision row.\n\n" + severalIDsHelp +
			"\n\n" + inOrderHelp + "\n\n" + approveSignInHelp +
			"\n\n" + delegatedReasonHelp("approve"),
		Args:          approveArgs(&workRef, &inOrder),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if inOrder {
				return runInOrder(cmd, inOrderOpts{
					WorkRef:           workRef,
					Reason:            reason,
					JSONOut:           jsonOut,
					BackplaneOverride: backplaneOverride,
				})
			}
			if len(args) > 1 {
				return runApproveMany(cmd, args, reason, jsonOut, backplaneOverride)
			}
			return runDecision(cmd, args[0], "approve", reason, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional rationale for the approval; the command help explains the Instructed-by: layout")
	cmd.Flags().StringVar(&workRef, workref.FilterFlag, "",
		"with --in-order: the change ticket whose pending requests to approve, oldest first")
	cmd.Flags().BoolVar(&inOrder, "in-order", false,
		"approve every pending request of --filter-work-ref oldest first; stop at the first failure")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw JSON response instead of summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// newRejectCmd returns the `meho approvals reject` command.
//
//	meho approvals reject <id> [--reason TEXT] [--json] [--backplane <url>]
//
// Role: operator. Rejects a pending approval request via
// POST /api/v1/approvals/{id}/decide. The backplane records the
// decision durably and broadcasts approval_decided; the paused agent
// run aborts off the broadcast (#1117 path).
func newRejectCmd() *cobra.Command {
	var (
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "reject <id>",
		Short: "Reject a pending approval request",
		Long: "reject calls POST /api/v1/approvals/{id}/decide with " +
			"decision=rejected to flip the request status, write the " +
			"decision audit row, and broadcast approval_decided. The " +
			"paused agent run aborts off the broadcast (T9 #1117). " +
			"Only pending requests can be rejected; any other status " +
			"returns 409. --reason attaches a human-readable " +
			"rationale to the rejection.\n\n" + delegatedReasonHelp("reject"),
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDecision(cmd, args[0], "reject", reason, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional rationale for the rejection; the command help explains the Instructed-by: layout")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw JSON response instead of summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

// runDecision is the shared implementation for approve and reject.
// It parses the operator's `<id>` arg as a UUID, dispatches the
// /decide POST through the typed client, then re-fetches the
// ApprovalRequestView so the renderer has the full post-decision
// shape (status, reviewed_by, decided_at).
func runDecision(
	cmd *cobra.Command,
	idArg, verb, reason string,
	jsonOut bool,
	backplaneOverride string,
) error {
	requestID, err := parseRequestID(idArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			jsonOut,
		)
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, jsonOut)
	if cerr != nil {
		return cerr
	}
	detail, err := postDecision(cmd.Context(), client, requestID, verb, reason)
	var noBrowser *auth.NoBrowserError
	if errors.As(err, &noBrowser) {
		return output.RenderError(cmd.ErrOrStderr(), noBrowserError(noBrowser.URL), jsonOut)
	}
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if detail == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ApprovalRequestView after decide"),
			jsonOut,
		)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), detail)
	}
	printDecision(cmd.OutOrStdout(), idArg, detail)
	return nil
}

// printDecision renders the post-decision summary: the new status, who
// decided and when, and -- for a delegated decision (#3692) -- who
// instructed the reviewer, their words, and the refs.
func printDecision(w io.Writer, idArg string, detail *api.ApprovalRequestView) {
	fmt.Fprintf(w, "approval_request %s → %s\n", idArg, string(detail.Status))
	if detail.ReviewedBy != nil {
		fmt.Fprintf(w, "reviewed by: %s\n", principalLabel(*detail.ReviewedBy, detail.ReviewedByName))
	}
	if detail.DecidedAt != nil {
		fmt.Fprintf(w, "decided at:  %s\n", *detail.DecidedAt)
	}
	if dl := detail.Delegation; dl != nil {
		fmt.Fprintf(w, "instructed by: %s\n", dl.InstructedBy)
		fmt.Fprintf(w, "instruction:   %q\n", dl.Instruction)
		fmt.Fprintf(w, "refs:          %s\n", strings.Join(dl.Refs, ", "))
	}
	// An approve runs the operation; say how the run ended (#3543).
	printRunOutcome(w, detail)
}

// postDecision calls POST /api/v1/approvals/{id}/decide via the
// generated client (G11.2-T5 operator-decision path: no params
// required; the backend flips the status, writes the decision audit
// row, and broadcasts the approval_decided event), then re-fetches
// the ApprovalRequestView via GET /api/v1/approvals/{id} so the
// caller can render the full post-decision shape. Returns a
// non-2xx response on the decide POST as an `*httpResponseError`
// without making the follow-up GET — there's no shape to render
// when the decision was rejected.
func postDecision(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
	verb, reason string,
) (*api.ApprovalRequestView, error) {
	// verb is "approve" or "reject"; backend wants the past-tense form.
	decision := "approved"
	if verb == "reject" {
		decision = "rejected"
	}
	body := api.DecideRequestBody{Decision: decision}
	if reason != "" {
		r := reason
		body.Reason = &r
	}
	var signIn *approvalSignIn
	if decision == "approved" {
		signIn = newApprovalSignIn(client, nil)
	}
	if _, err := sendDecide(ctx, client, requestID, body, signIn, false); err != nil {
		return nil, err
	}
	// Decision committed; fetch the full ApprovalRequestView for
	// rendering. fetchDetail already retries on 401 (rare here since
	// the decide call just succeeded, but defensive against a
	// token-rotation window between the two calls).
	return fetchDetail(ctx, client, requestID)
}

// sendDecide posts one decision to POST /api/v1/approvals/{id}/decide.
// A non-2xx answer comes back as an `*httpResponseError`. Shared by the
// single-id path (postDecision), the several-ids path (runApproveMany)
// and the in-order path (runInOrder), which reads the response body for
// dispatch_status.
//
// A reject uses the stored login, with a one-shot refresh on 401. An
// approve uses the run's approval sign-in instead (#5515): the stored
// login never approves. destructive says whether the request's
// operation is destructive; such a request gets a sign-in of its own.
func sendDecide(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
	body api.DecideRequestBody,
	signIn *approvalSignIn,
	destructive bool,
) (*api.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostResponse, error) {
	if body.Decision == "approved" && signIn != nil {
		return sendApprove(ctx, client, requestID, body, signIn, destructive)
	}
	resp, err := client.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostWithResponse(ctx, requestID, nil, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 && !needsFreshSignIn(resp.HTTPResponse) {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		resp, err = client.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostWithResponse(ctx, requestID, nil, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp, nil
}

// sendApprove posts one approve with the token of the approval sign-in.
// When MEHO refuses that sign-in (the RFC 9470 challenge: too old, or
// used up by a destructive request), it opens one new sign-in and tries
// once more.
func sendApprove(
	ctx context.Context,
	client *api.AuthedClient,
	requestID uuid.UUID,
	body api.DecideRequestBody,
	signIn *approvalSignIn,
	destructive bool,
) (*api.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostResponse, error) {
	post := func() (*api.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostResponse, error) {
		token, err := signIn.bearer(ctx, destructive)
		if err != nil {
			return nil, err
		}
		withSignIn := func(_ context.Context, req *http.Request) error {
			req.Header.Set("Authorization", "Bearer "+token)
			return nil
		}
		return client.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostWithResponse(ctx, requestID, nil, body, withSignIn)
	}
	resp, err := post()
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 && needsFreshSignIn(resp.HTTPResponse) {
		signIn.forget()
		if resp, err = post(); err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	signIn.approved(destructive)
	return resp, nil
}

// needsFreshSignIn reports whether a 401 asks for a newer sign-in: the
// RFC 9470 §3 challenge error="insufficient_user_authentication".
func needsFreshSignIn(resp *http.Response) bool {
	if resp == nil {
		return false
	}
	for _, v := range resp.Header.Values("WWW-Authenticate") {
		if strings.Contains(v, `error="insufficient_user_authentication"`) ||
			strings.Contains(v, "error=insufficient_user_authentication") {
			return true
		}
	}
	return false
}

// noBrowserError is the message when no browser could be opened for
// the fresh sign-in. It points to the web interface and gives the link.
func noBrowserError(signInURL string) *output.StructuredError {
	return output.AuthExpired(
		"this approval needs a fresh sign-in, and no browser could be opened. " +
			"Approve in the web interface: that is the normal place to approve. " +
			"Or run this command on a computer where a browser can open. " +
			"Sign-in link: " + signInURL)
}
