// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// RunStatus mirrors the backend AgentRunStatusResponse pydantic model
// (`backend/src/meho_backplane/api/v1/agent_runs.py`). Output / Error are
// populated only once the run reaches a terminal state.
type RunStatus struct {
	RunID    string         `json:"run_id"`
	Status   string         `json:"status"`
	Turns    int            `json:"turns"`
	Provider *string        `json:"provider"`
	Model    *string        `json:"model"`
	Output   map[string]any `json:"output"`
	Error    *string        `json:"error"`
}

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
		"emit the raw status JSON instead of the human summary")
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
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	status, err := getRunStatus(cmd.Context(), backplaneURL, opts.Handle)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), status)
	}
	printRunStatus(cmd.OutOrStdout(), status)
	return nil
}

// buildRunStatusPath assembles the GET path. Exposed for unit tests so URL
// encoding of the handle stays covered.
func buildRunStatusPath(handle string) string {
	return "/api/v1/agents/runs/" + url.PathEscape(handle)
}

func getRunStatus(ctx context.Context, backplaneURL, handle string) (*RunStatus, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildRunStatusPath(handle), nil)
	if err != nil {
		return nil, err
	}
	var out RunStatus
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode agent run-status response: %w", err)
	}
	return &out, nil
}

// printRunStatus renders a run status as a key-value summary.
func printRunStatus(w io.Writer, s *RunStatus) {
	if s == nil {
		return
	}
	fmt.Fprintf(w, "%-16s %s\n", "run_id:", s.RunID)
	fmt.Fprintf(w, "%-16s %s\n", "status:", s.Status)
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
		if encoded, err := json.Marshal(s.Output); err == nil {
			fmt.Fprintf(w, "%-16s %s\n", "output:", string(encoded))
		}
	}
}
