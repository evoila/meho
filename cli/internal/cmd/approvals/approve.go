// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newApproveCmd returns the `meho approvals approve` command.
//
//	meho approvals approve <id> [--reason TEXT] [--json] [--backplane <url>]
//
// Role: operator. Approves a pending approval request via
// POST /api/v1/approvals/{id}/decide. The backplane records the
// decision durably and broadcasts approval_decided; the paused agent
// run resumes off the broadcast (#1117 path).
func newApproveCmd() *cobra.Command {
	var (
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "approve <id>",
		Short: "Approve a pending approval request",
		Long: "approve calls POST /api/v1/approvals/{id}/decide with " +
			"decision=approved to flip the request status, write the " +
			"decision audit row, and broadcast approval_decided. The " +
			"paused agent run resumes off the broadcast (T9 #1117). " +
			"Only pending requests can be approved; any other status " +
			"returns 409. --reason attaches a human-readable " +
			"rationale to the decision row.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDecision(cmd, args[0], "approve", reason, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional rationale for the approval")
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
			"rationale to the rejection.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDecision(cmd, args[0], "reject", reason, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&reason, "reason", "", "optional rationale for the rejection")
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
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "approval_request %s → %s\n", idArg, string(detail.Status))
	if detail.ReviewedBy != nil {
		fmt.Fprintf(w, "reviewed by: %s\n", *detail.ReviewedBy)
	}
	if detail.DecidedAt != nil {
		fmt.Fprintf(w, "decided at:  %s\n", *detail.DecidedAt)
	}
	return nil
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
	resp, err := client.DecideApprovalRequestApiV1ApprovalsRequestIdDecidePostWithResponse(ctx, requestID, nil, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == 401 {
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
	// Decision committed; fetch the full ApprovalRequestView for
	// rendering. fetchDetail already retries on 401 (rare here since
	// the decide call just succeeded, but defensive against a
	// token-rotation window between the two calls).
	return fetchDetail(ctx, client, requestID)
}
