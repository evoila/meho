// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"context"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newReassignRunCmd returns the `meho runbook reassign` command.
//
// CLI shape (per issue #1319):
//
//	meho runbook reassign <run_id> --to <operator-sub> [--json]
//	  [--backplane URL]
//
// Wraps POST /api/v1/runbooks/runs/{run_id}/reassign. Role:
// tenant_admin (enforced at the route gate; the service is
// role-agnostic). Reassign is the load-bearing escalation knob (per
// Initiative #1198, the only way for a senior to take over a
// junior's stuck run -- there is no force_advance, no admin bypass
// on `next`).
//
// Exit codes:
//   - 0   reassigned (200)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 400 run_already_terminal,
//     404 run_not_found, 422 empty_new_assignee)
//   - 5   insufficient_role (route gate refuses operator callers)
func newReassignRunCmd() *cobra.Command {
	var (
		newAssignee       string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "reassign <run_id>",
		Short: "Transfer ownership of an in-progress run (tenant_admin)",
		Long: "reassign calls POST " +
			"/api/v1/runbooks/runs/{run_id}/reassign. Tenant_admin only.\n\n" +
			"--to <operator-sub> is required: the subject identifier of " +
			"the new owner. After reassign, only the new assignee can " +
			"call `meho runbook next`.\n\n" +
			"Use when: a junior is stuck on a step and the senior needs " +
			"to take the controls. The junior should `meho runbook " +
			"abort` if the procedure itself is broken; reassign is for " +
			"\"this operator needs to step in,\" not for \"this " +
			"procedure is broken.\"",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runReassignRun(cmd, reassignRunOptions{
				RunID:             args[0],
				NewAssignee:       newAssignee,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&newAssignee, "to", "",
		"required: operator subject identifier (`sub`) to transfer ownership to")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ReassignRunResponse JSON instead of the human confirmation")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type reassignRunOptions struct {
	RunID             string
	NewAssignee       string
	JSONOut           bool
	BackplaneOverride string
}

func runReassignRun(cmd *cobra.Command, opts reassignRunOptions) error {
	if opts.RunID == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("reassign requires a non-empty <run_id> argument"),
			opts.JSONOut,
		)
	}
	runID, err := uuid.Parse(opts.RunID)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid run_id %q: %v", opts.RunID, err)),
			opts.JSONOut,
		)
	}
	newAssignee := strings.TrimSpace(opts.NewAssignee)
	if newAssignee == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("reassign requires --to <operator-sub> (the new owner's subject identifier)"),
			opts.JSONOut,
		)
	}
	backplaneURL, berr := backplane.Resolve(opts.BackplaneOverride)
	if berr != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(berr), opts.JSONOut)
	}
	resp, rerr := postReassignRun(cmd.Context(), backplaneURL, runID, newAssignee)
	if rerr != nil {
		return renderRequestError(cmd, backplaneURL, rerr, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a ReassignRunResponse payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	fmt.Fprintf(cmd.OutOrStdout(),
		"Reassigned run %s to %s (reassigned_at=%s)\n",
		resp.JSON200.RunId,
		resp.JSON200.AssignedTo,
		resp.JSON200.ReassignedAt.UTC().Format("2006-01-02T15:04:05Z"),
	)
	return nil
}

func postReassignRun(
	ctx context.Context,
	backplaneURL string,
	runID uuid.UUID,
	newAssignee string,
) (*api.ReassignRunApiV1RunbooksRunsRunIdReassignPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	body := api.ReassignRunRequest{NewAssignee: newAssignee}
	params := &api.ReassignRunApiV1RunbooksRunsRunIdReassignPostParams{}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ReassignRunApiV1RunbooksRunsRunIdReassignPostResponse, error) {
			return authed.ReassignRunApiV1RunbooksRunsRunIdReassignPostWithResponse(
				ctx, runID, params, body,
			)
		},
		func(r *api.ReassignRunApiV1RunbooksRunsRunIdReassignPostResponse) int { return r.StatusCode() },
	)
}
