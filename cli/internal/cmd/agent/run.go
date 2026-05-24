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

// RunRequest mirrors the backend AgentRunRequest pydantic model
// (`backend/src/meho_backplane/api/v1/agent_runs.py`). `Async` uses the
// `async` wire key (a Python keyword on the backend, plain on the wire).
type RunRequest struct {
	Input string `json:"input"`
	Async bool   `json:"async"`
}

// RunResult mirrors the backend AgentRunResultResponse (a terminal sync
// run, HTTP 200) and AgentRunHandleResponse (an async / converted-to-async
// run, HTTP 202) combined — only the fields present in a given response are
// populated, so unset pointer fields distinguish the two shapes. Output is
// a free-shaped JSON object (the run's structured / {"text": ...} result).
type RunResult struct {
	RunID            string         `json:"run_id"`
	Status           string         `json:"status"`
	Output           map[string]any `json:"output,omitempty"`
	Error            *string        `json:"error,omitempty"`
	ConvertedToAsync bool           `json:"converted_to_async,omitempty"`
}

// newRunCmd returns the `meho agent run` command.
//
//	meho agent run <name> --input TEXT [--async] [--json] [--backplane <url>]
//
// Role: operator. Runs the named agent via POST /api/v1/agents/{name}/run.
// Sync (default) blocks up to the server-side timeout and prints the final
// output; a run that exceeds the timeout, or --async, prints the run handle
// to poll with `meho agent run-status`.
func newRunCmd() *cobra.Command {
	var (
		input             string
		asyncRun          bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "run <name>",
		Short: "Run an agent (sync block-and-return, or --async for a handle)",
		Long: "run calls POST /api/v1/agents/{name}/run. Without --async it " +
			"blocks up to the server-side timeout and prints the final " +
			"output; a long run converts to async and prints a handle. With " +
			"--async it returns a handle immediately. Poll a handle with " +
			"`meho agent run-status <handle>` or stream a fresh run's events " +
			"with `meho agent run-events <name>`. --json emits the raw " +
			"response for scripting.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRun(cmd, runOptions{
				Name:              args[0],
				Input:             input,
				Async:             asyncRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&input, "input", "",
		"the user prompt to run the agent on (required)")
	cmd.Flags().BoolVar(&asyncRun, "async", false,
		"return a run handle immediately instead of blocking for the result")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw run response JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runOptions struct {
	Name              string
	Input             string
	Async             bool
	JSONOut           bool
	BackplaneOverride string
}

func runRun(cmd *cobra.Command, opts runOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("run requires a non-empty <name> argument"), opts.JSONOut)
	}
	if opts.Input == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("run requires a non-empty --input"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, err := postRun(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printRunResult(cmd.OutOrStdout(), result)
	return nil
}

// buildRunPath assembles the POST path. Exposed for unit tests so URL
// encoding of names with dots / hyphens stays covered.
func buildRunPath(name string) string {
	return "/api/v1/agents/" + url.PathEscape(name) + "/run"
}

func postRun(ctx context.Context, backplaneURL string, opts runOptions) (*RunResult, error) {
	body, err := json.Marshal(RunRequest{Input: opts.Input, Async: opts.Async})
	if err != nil {
		return nil, fmt.Errorf("encode run request: %w", err)
	}
	raw, err := doAuthedRequest(ctx, backplaneURL, "POST", buildRunPath(opts.Name), body)
	if err != nil {
		return nil, err
	}
	var out RunResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode agent run response: %w", err)
	}
	return &out, nil
}

// printRunResult renders a run response as a key-value summary. A terminal
// run shows its output; an async / converted run shows the handle and a
// hint to poll it.
func printRunResult(w io.Writer, r *RunResult) {
	if r == nil {
		return
	}
	fmt.Fprintf(w, "%-16s %s\n", "run_id:", r.RunID)
	fmt.Fprintf(w, "%-16s %s\n", "status:", r.Status)
	if r.ConvertedToAsync {
		fmt.Fprintf(w, "%-16s %s\n", "note:", "run exceeded the sync timeout; converted to async")
	}
	switch {
	case r.Error != nil && *r.Error != "":
		fmt.Fprintf(w, "%-16s %s\n", "error:", *r.Error)
	case r.Output != nil:
		encoded, err := json.Marshal(r.Output)
		if err == nil {
			fmt.Fprintf(w, "%-16s %s\n", "output:", string(encoded))
		}
	default:
		fmt.Fprintf(w, "%-16s %s\n", "hint:",
			fmt.Sprintf("poll with `meho agent run-status %s`", r.RunID))
	}
}
