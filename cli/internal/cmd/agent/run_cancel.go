// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRunCancelCmd returns the `meho agent run-cancel` command.
//
//	meho agent run-cancel <handle> [--json] [--backplane <url>]
//
// Role: operator. Cancels a non-terminal run via
// POST /api/v1/agents/runs/{handle}/cancel. The run transitions to
// cancelled and the updated summary is rendered. A 404 means no such run
// in your tenant; a 409 means the run already reached a terminal state
// (succeeded / failed / cancelled) and cannot be cancelled.
func newRunCancelCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "run-cancel <handle>",
		Short: "Cancel a non-terminal agent run by handle",
		Long: "run-cancel calls POST /api/v1/agents/runs/{handle}/cancel and " +
			"stops a pending / running / awaiting_approval run: the run " +
			"transitions to cancelled and the updated summary is rendered. " +
			"The handle is the run id printed by `meho agent run`. A 404 " +
			"means no such run in your tenant; a 409 means the run already " +
			"reached a terminal state (succeeded / failed / cancelled) and " +
			"cannot be cancelled. --json emits the raw " +
			"AgentRunSummaryResponse for scripting.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRunCancel(cmd, runCancelOptions{
				Handle:            args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw AgentRunSummaryResponse JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runCancelOptions struct {
	Handle            string
	JSONOut           bool
	BackplaneOverride string
}

func runRunCancel(cmd *cobra.Command, opts runCancelOptions) error {
	if opts.Handle == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("run-cancel requires a non-empty <handle> argument"), opts.JSONOut)
	}
	// The generated client takes the handle as a uuid.UUID (the OpenAPI
	// spec marks the path param as format=uuid), so parse the operator's
	// string argument once at the verb edge and surface a clean
	// CLI-side error before the request is built.
	handle, err := uuid.Parse(opts.Handle)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid <handle>: %v", err)), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := cancelRun(cmd.Context(), backplaneURL, handle)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printRunSummary(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func cancelRun(ctx context.Context, backplaneURL string, handle uuid.UUID) (*api.CancelRunApiV1AgentsRunsHandleCancelPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.CancelRunApiV1AgentsRunsHandleCancelPostResponse, error) {
			return authed.CancelRunApiV1AgentsRunsHandleCancelPostWithResponse(ctx, handle, nil)
		},
		func(r *api.CancelRunApiV1AgentsRunsHandleCancelPostResponse) int { return r.StatusCode() },
	)
}

// printRunSummary renders an AgentRunSummaryResponse as a key-value
// summary — the shape the cancel route returns for the updated run.
func printRunSummary(w io.Writer, s *api.AgentRunSummaryResponse) {
	if s == nil {
		return
	}
	fmt.Fprintf(w, "%-16s %s\n", "run_id:", s.RunId.String())
	fmt.Fprintf(w, "%-16s %s\n", "status:", string(s.Status))
	fmt.Fprintf(w, "%-16s %s\n", "trigger:", s.Trigger)
	fmt.Fprintf(w, "%-16s %s\n", "model_tier:", s.ModelTier)
	fmt.Fprintf(w, "%-16s %d\n", "turns:", s.Turns)
	if s.Provider != nil {
		fmt.Fprintf(w, "%-16s %s\n", "provider:", *s.Provider)
	}
	if s.Model != nil {
		fmt.Fprintf(w, "%-16s %s\n", "model:", *s.Model)
	}
	if s.WorkRef != nil && *s.WorkRef != "" {
		fmt.Fprintf(w, "%-16s %s\n", "work_ref:", *s.WorkRef)
	}
	fmt.Fprintf(w, "%-16s %s\n", "created_at:", s.CreatedAt.UTC().Format("2006-01-02T15:04:05Z"))
	if s.EndedAt != nil {
		fmt.Fprintf(w, "%-16s %s\n", "ended_at:", s.EndedAt.UTC().Format("2006-01-02T15:04:05Z"))
	}
}
