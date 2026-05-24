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
	"net/url"
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

// buildRunEventsPath assembles the SSE POST path. Exposed for unit tests.
func buildRunEventsPath(name string) string {
	return "/api/v1/agents/" + url.PathEscape(name) + "/run/events"
}

// streamRunEvents opens the SSE connection and prints each event frame. The
// bearer is injected via the shared authed client; a 401 surfaces as an
// *httpError so renderRequestError maps it to auth_expired. Unlike the
// broadcast `status --watch` verb, this is a single-shot stream (one run),
// so there is no reconnect loop — the run ends, the stream ends.
func streamRunEvents(cmd *cobra.Command, backplaneURL string, opts runEventsOptions) error {
	body, err := json.Marshal(RunRequest{Input: opts.Input})
	if err != nil {
		return fmt.Errorf("encode run-events request: %w", err)
	}
	authed, err := api.NewAuthedClient(cmd.Context(), backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return err
	}
	bearer := authed.AccessToken()
	if bearer == "" {
		return errMissingAccessToken
	}
	resp, err := sendSSERequest(cmd.Context(), authed.HTTPClient(), backplaneURL, opts.Name, bearer, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, responseBodyCap))
		return &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return printSSEStream(cmd.OutOrStdout(), resp.Body, opts.JSONOut)
}

func sendSSERequest(
	ctx context.Context,
	client *http.Client,
	backplaneURL, name, bearer string,
	body []byte,
) (*http.Response, error) {
	req, err := http.NewRequestWithContext(
		ctx, "POST", backplaneURL+buildRunEventsPath(name), strings.NewReader(string(body)),
	)
	if err != nil {
		return nil, fmt.Errorf("build run-events request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Content-Type", "application/json")
	return client.Do(req)
}

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
