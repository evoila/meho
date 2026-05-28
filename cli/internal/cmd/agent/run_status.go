// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRunStatusCmd returns the `meho agent run-status` command.
//
//	meho agent run-status <handle> [--json] [--backplane <url>]
//
// Role: operator. Polls a run's durable status via
// GET /api/v1/agents/runs/{handle}. Reads the durable run record, so it
// works after the call that started the run returned.
func newRunStatusCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "run-status <handle>",
		Short: "Poll an agent run's status by handle",
		Long: "run-status calls GET /api/v1/agents/runs/{handle} and renders " +
			"the run's durable status (status, turns, provider, model, and " +
			"the output/error once terminal). The handle is the run id " +
			"printed by `meho agent run`. A 404 means no such run in your " +
			"tenant. --json emits the raw status for scripting (poll-loops).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRunStatus(cmd, runStatusOptions{
				Handle:            args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw AgentRunStatusResponse JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runStatusOptions struct {
	Handle            string
	JSONOut           bool
	BackplaneOverride string
}

func runRunStatus(cmd *cobra.Command, opts runStatusOptions) error {
	if opts.Handle == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("run-status requires a non-empty <handle> argument"), opts.JSONOut)
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
	resp, err := getRunStatus(cmd.Context(), backplaneURL, handle)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printRunStatus(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

func getRunStatus(ctx context.Context, backplaneURL string, handle uuid.UUID) (*api.GetRunStatusApiV1AgentsRunsHandleGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.GetRunStatusApiV1AgentsRunsHandleGetResponse, error) {
			return authed.GetRunStatusApiV1AgentsRunsHandleGetWithResponse(ctx, handle, nil)
		},
		func(r *api.GetRunStatusApiV1AgentsRunsHandleGetResponse) int { return r.StatusCode() },
	)
}

// printRunStatus renders a run status as a key-value summary.
func printRunStatus(w io.Writer, s *api.AgentRunStatusResponse) {
	if s == nil {
		return
	}
	fmt.Fprintf(w, "%-16s %s\n", "run_id:", s.RunId.String())
	fmt.Fprintf(w, "%-16s %s\n", "status:", string(s.Status))
	fmt.Fprintf(w, "%-16s %d\n", "turns:", s.Turns)
	if s.Provider != nil {
		fmt.Fprintf(w, "%-16s %s\n", "provider:", *s.Provider)
	}
	if s.Model != nil {
		fmt.Fprintf(w, "%-16s %s\n", "model:", *s.Model)
	}
	if s.Error != nil && *s.Error != "" {
		fmt.Fprintf(w, "%-16s %s\n", "error:", *s.Error)
	}
	if s.Output != nil {
		if encoded, err := json.Marshal(*s.Output); err == nil {
			fmt.Fprintf(w, "%-16s %s\n", "output:", string(encoded))
		}
	}
}
