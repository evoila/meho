// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newApproveCmd returns the `meho approvals approve` command.
//
//	meho approvals approve <id> [--reason TEXT] [--json] [--backplane <url>]
//
// Role: operator. Approves a pending approval request via
// POST /api/v1/approvals/{id}/approve. The backplane resumes the
// paused agent run and records the decision as an audit row.
func newApproveCmd() *cobra.Command {
	var (
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "approve <id>",
		Short: "Approve a pending approval request",
		Long: "approve calls POST /api/v1/approvals/{id}/approve to " +
			"flip the request status to approved, resume the paused " +
			"agent run (T4 path), and record the decision as an audit " +
			"row. Only pending requests can be approved; any other " +
			"status returns 409. --reason attaches a human-readable " +
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
// POST /api/v1/approvals/{id}/reject. The backplane aborts the paused
// agent run and records the decision as an audit row.
func newRejectCmd() *cobra.Command {
	var (
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "reject <id>",
		Short: "Reject a pending approval request",
		Long: "reject calls POST /api/v1/approvals/{id}/reject to " +
			"flip the request status to rejected, abort the paused " +
			"agent run (T4 path), and record the decision as an audit " +
			"row. Only pending requests can be rejected; any other " +
			"status returns 409. --reason attaches a human-readable " +
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
func runDecision(
	cmd *cobra.Command,
	id, verb, reason string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	detail, err := postDecision(cmd.Context(), backplaneURL, id, verb, reason)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), detail)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "approval_request %s → %s\n", id, detail.Status)
	if detail.ReviewedBy != nil {
		fmt.Fprintf(w, "reviewed by: %s\n", *detail.ReviewedBy)
	}
	if detail.DecidedAt != nil {
		fmt.Fprintf(w, "decided at:  %s\n", *detail.DecidedAt)
	}
	return nil
}

// postDecision calls POST /api/v1/approvals/{id}/decide (G11.2-T5
// operator-decision path: no params required; the backend flips the
// status, writes the decision audit row, and broadcasts the
// approval_decided event). After the decision commits, the function
// fetches GET /api/v1/approvals/{id} so the caller can render the
// full ApprovalRequestView (status, reviewed_by, decided_at).
func postDecision(
	ctx context.Context,
	backplaneURL, id, verb, reason string,
) (*ApprovalDetail, error) {
	// verb is "approve" or "reject"; backend wants the past-tense form.
	decision := "approved"
	if verb == "reject" {
		decision = "rejected"
	}
	body := decisionBody{Decision: decision, Reason: reason}
	bodyJSON, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal decision body: %w", err)
	}
	decidePath := fmt.Sprintf("/api/v1/approvals/%s/decide", id)
	if _, err := doAuthedRequest(ctx, backplaneURL, "POST", decidePath, bodyJSON); err != nil {
		return nil, err
	}
	// Fetch the full view for rendering.
	showPath := fmt.Sprintf("/api/v1/approvals/%s", id)
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", showPath, nil)
	if err != nil {
		return nil, err
	}
	var out ApprovalDetail
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode show response after decide: %w", err)
	}
	return &out, nil
}
