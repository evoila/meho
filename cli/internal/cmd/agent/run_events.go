// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newRunEventsCmd returns the `meho agent run-events` command.
//
//	meho agent run-events <name> --input TEXT [--json] [--backplane <url>]
//
// Role: operator. Streams a fresh run's events via Server-Sent Events from
// POST /api/v1/agents/{name}/run/events, printing one line per event
// (turn / tool_call / tool_result / final / error) until the stream ends.
// The connection lifetime is the run's lifetime — one connection, one run.
func newRunEventsCmd() *cobra.Command {
	var (
		input             string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "run-events <name>",
		Short: "Stream a fresh agent run's events over SSE",
		Long: "run-events opens an SSE stream to " +
			"POST /api/v1/agents/{name}/run/events and prints one line per " +
			"event (turn / tool_call / tool_result / final / error) as the " +
			"loop runs. The connection lives for the run's lifetime. " +
			"--json emits one raw JSON object per event for scripting; " +
			"omit it for a compact human line per event. Ctrl-C ends the " +
			"stream.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runRunEvents(cmd, runEventsOptions{
				Name:              args[0],
				Input:             input,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&input, "input", "",
		"the user prompt to run the agent on (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit one raw JSON object per event instead of a compact human line")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runEventsOptions struct {
	Name              string
	Input             string
	JSONOut           bool
	BackplaneOverride string
}

func runRunEvents(cmd *cobra.Command, opts runEventsOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("run-events requires a non-empty <name> argument"), opts.JSONOut)
	}
	if opts.Input == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("run-events requires a non-empty --input"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	if err := streamRunEvents(cmd, backplaneURL, opts); err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	return nil
}

// streamRunEvents opens the SSE connection and prints each event frame.
// The endpoint is the streaming variant of the agent-run RPC, so it
// uses the generated client's non-`*WithResponse` method
// (RunAgentEventsApiV1AgentsNameRunEventsPost) — that signature returns
// the raw `*http.Response` so the body stays unbuffered for the SSE
// scanner. The `*WithResponse` variant would buffer the entire body
// and break event streaming.
//
// 401 retry runs once: a stale bearer triggers a refresh and one
// re-issue, mirroring api.AuthedClient.GetHealth's contract. The
// Accept header is overridden via a per-call RequestEditorFn so the
// SSE response carries the right Content-Type negotiation.
func streamRunEvents(cmd *cobra.Command, backplaneURL string, opts runEventsOptions) error {
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return err
	}
	body := api.AgentRunRequest{Input: opts.Input}
	editor := func(_ context.Context, req *http.Request) error {
		req.Header.Set("Accept", "text/event-stream")
		return nil
	}
	resp, err := authed.RunAgentEventsApiV1AgentsNameRunEventsPost(
		cmd.Context(), opts.Name, nil, body, editor,
	)
	if err != nil {
		return err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(cmd.Context()); rerr != nil {
			resp.Body.Close()
			return rerr
		}
		resp.Body.Close()
		resp, err = authed.RunAgentEventsApiV1AgentsNameRunEventsPost(
			cmd.Context(), opts.Name, nil, body, editor,
		)
		if err != nil {
			return err
		}
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Drain an error body into the renderer; cap the read so an
		// adversarial / oversized error body can't pin the verb.
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, errBodyCap))
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode, raw, opts.JSONOut)
	}
	return printSSEStream(cmd.OutOrStdout(), resp.Body, opts.JSONOut)
}

// errBodyCap bounds the error-body read on a non-2xx SSE handshake.
// Symmetric with the buffered envelope's 1 MiB ceiling.
const errBodyCap int64 = 1 << 20

// printSSEStream parses the SSE frames off r and prints one line per event.
// SSE frames are `event: <kind>` + `data: <json>` separated by a blank
// line; a `:` comment line (heartbeat) is ignored. Parsing is deliberately
// minimal — enough for the one event-name + one data-line frames the
// backend emits.
func printSSEStream(w io.Writer, r io.Reader, jsonOut bool) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	var eventName, data string
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case line == "":
			// End of one frame — emit and reset.
			if eventName != "" || data != "" {
				printSSEEvent(w, eventName, data, jsonOut)
			}
			eventName, data = "", ""
		case strings.HasPrefix(line, ":"):
			// SSE comment (heartbeat) — ignore.
		case strings.HasPrefix(line, "event:"):
			eventName = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			data = strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("read SSE stream: %w", err)
	}
	return nil
}

// printSSEEvent renders one parsed SSE frame. --json prints the data object
// with the event kind merged in; otherwise a compact `kind: data` line.
func printSSEEvent(w io.Writer, eventName, data string, jsonOut bool) {
	if jsonOut {
		var obj map[string]any
		if err := json.Unmarshal([]byte(data), &obj); err != nil {
			obj = map[string]any{"raw": data}
		}
		obj["event"] = eventName
		if encoded, err := json.Marshal(obj); err == nil {
			fmt.Fprintln(w, string(encoded))
			return
		}
	}
	fmt.Fprintf(w, "%-12s %s\n", eventName, data)
}
