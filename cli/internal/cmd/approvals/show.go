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

// newShowCmd returns the `meho approvals show` command.
//
//	meho approvals show <id> [--json] [--backplane <url>]
//
// Role: operator. Fetches a single approval request via
// GET /api/v1/approvals/{id} and renders it with proposed_effect and
// the elicitation_url so the operator knows where to POST a decision.
func newShowCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <id>",
		Short: "Inspect a pending approval request",
		Long: "show calls GET /api/v1/approvals/{id} and renders the " +
			"full approval request detail: the proposed operation " +
			"(connector, op, target, proposed_effect), its current " +
			"status, the principal that triggered it, and the " +
			"elicitation_url MCP clients can use to post a decision. " +
			"--json emits the raw JSON envelope.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, args[0], jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw JSON instead of the human-readable view")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from `meho login`)")
	return cmd
}

func runShow(cmd *cobra.Command, id string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	detail, err := fetchDetail(cmd.Context(), backplaneURL, id)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), detail)
	}
	printDetail(cmd, detail)
	return nil
}

func fetchDetail(ctx context.Context, backplaneURL, id string) (*ApprovalDetail, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", "/api/v1/approvals/"+id, nil)
	if err != nil {
		return nil, err
	}
	var out ApprovalDetail
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode approval detail response: %w", err)
	}
	return &out, nil
}

func printDetail(cmd *cobra.Command, d *ApprovalDetail) {
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "ID:           %s\n", d.ID)
	fmt.Fprintf(w, "Status:       %s\n", d.Status)
	fmt.Fprintf(w, "Connector:    %s\n", d.ConnectorID)
	fmt.Fprintf(w, "Operation:    %s\n", d.OpID)
	if d.TargetID != nil {
		fmt.Fprintf(w, "Target:       %s\n", *d.TargetID)
	}
	fmt.Fprintf(w, "Principal:    %s\n", d.PrincipalSub)
	if d.PrincipalAct != nil {
		fmt.Fprintf(w, "Acting as:    %s\n", *d.PrincipalAct)
	}
	if d.RunID != nil {
		fmt.Fprintf(w, "Agent run:    %s\n", *d.RunID)
	}
	fmt.Fprintf(w, "Params hash:  %s\n", d.ParamsHash)
	fmt.Fprintf(w, "Created:      %s\n", d.CreatedAt)
	if d.ExpiresAt != nil {
		fmt.Fprintf(w, "Expires:      %s\n", *d.ExpiresAt)
	}
	if d.ReviewedBy != nil {
		fmt.Fprintf(w, "Reviewed by:  %s\n", *d.ReviewedBy)
	}
	if d.DecidedAt != nil {
		fmt.Fprintf(w, "Decided at:   %s\n", *d.DecidedAt)
	}
	if d.ProposedEffect != nil {
		b, _ := json.MarshalIndent(d.ProposedEffect, "  ", "  ")
		fmt.Fprintf(w, "Effect:\n  %s\n", string(b))
	}
}
