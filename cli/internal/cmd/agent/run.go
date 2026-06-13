// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// runResponse models the wire body of POST /api/v1/agents/{name}/run.
// The endpoint is one of the small set of MEHO routes whose response
// is a discriminated union of two unrelated Pydantic models —
// AgentRunResultResponse on a terminal sync run (HTTP 200) and
// AgentRunHandleResponse on an async / converted-to-async run
// (HTTP 202) — emitted via a JSONResponse without a single
// FastAPI response_model. The OpenAPI snapshot therefore renders
// the response as a free-shape `Any` at 200 only, and the generated
// `RunAgentApiV1AgentsNameRunPostResponse.JSON200` lands as
// `*interface{}`. To keep the CLI's typed-printing contract we
// re-decode the response body off the typed envelope into this
// union; the field set covers both wire shapes (omitempty on
// terminal-only / handle-only fields lets the marshaler still emit a
// minimal --json payload).
//
// Code-quality-allow: tracked under the "fix Run endpoint OpenAPI
// surface to use a discriminated union of two response models" follow-
// up issue (Initiative G0.12 #1118 adjacent finding) — that's a
// backend FastAPI change outside the blast radius of this consumer-
// side migration.
type runResponse struct {
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
		workRef           string
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
				WorkRef:           workRef,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&input, "input", "",
		"the user prompt to run the agent on (required)")
	cmd.Flags().BoolVar(&asyncRun, "async", false,
		"return a run handle immediately instead of blocking for the result")
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"external change-ticket reference to bind the run to (e.g. gh:evoila/meho#11); "+
			"filterable via `meho agent run-list --work-ref`")
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
	WorkRef           string
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
	resp, err := postRun(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// /run replies with 200 on a terminal sync outcome and 202 on a
	// converted-to-async / fresh-async run; both are success here.
	status := resp.StatusCode()
	if status != http.StatusOK && status != http.StatusAccepted {
		return renderHTTPStatus(cmd, backplaneURL, status, resp.Body, opts.JSONOut)
	}
	var result runResponse
	if err := json.Unmarshal(resp.Body, &result); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode agent run response: %v", err)), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printRunResult(cmd.OutOrStdout(), &result)
	return nil
}

func postRun(ctx context.Context, backplaneURL string, opts runOptions) (*api.RunAgentApiV1AgentsNameRunPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	asyncFlag := opts.Async
	body := api.AgentRunRequest{
		Input: opts.Input,
		Async: &asyncFlag,
	}
	params := &api.RunAgentApiV1AgentsNameRunPostParams{}
	if opts.WorkRef != "" {
		wr := opts.WorkRef
		params.MehoWorkRef = &wr
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RunAgentApiV1AgentsNameRunPostResponse, error) {
			return authed.RunAgentApiV1AgentsNameRunPostWithResponse(ctx, opts.Name, params, body)
		},
		func(r *api.RunAgentApiV1AgentsNameRunPostResponse) int { return r.StatusCode() },
	)
}

// printRunResult renders a run response as a key-value summary. A terminal
// run shows its output; an async / converted run shows the handle and a
// hint to poll it.
func printRunResult(w io.Writer, r *runResponse) {
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
