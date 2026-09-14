// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

func newRecentCmd() *cobra.Command {
	var opts recentOptions
	cmd := &cobra.Command{
		Use:   "recent",
		Short: "Read recent broadcast events for the operator's tenant",
		Args:  cobra.NoArgs, SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error { return runRecent(cmd, opts) },
	}
	cmd.Flags().StringVar(&opts.Cursor, "cursor", "", "forward cursor (ISO-8601 timestamp or stream id)")
	cmd.Flags().StringVar(&opts.OpClass, "op-class", "", "exact op class filter")
	cmd.Flags().StringVar(&opts.Principal, "principal", "", "exact principal filter")
	cmd.Flags().StringVar(&opts.Target, "target", "", "exact target filter")
	cmd.Flags().StringVar(&opts.ActorSub, "actor-sub", "", "exact delegated-agent filter")
	cmd.Flags().StringVar(&opts.WorkRef, "work-ref", "", "exact work-reference filter")
	cmd.Flags().BoolVar(&opts.ActiveOnly, "active-only", false, "exclude expired TTL claims")
	cmd.Flags().IntVar(&opts.Limit, "limit", 100, "maximum events (1-1000)")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "emit JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "", "backplane URL")
	return cmd
}

type recentOptions struct {
	Cursor, OpClass, Principal, Target, ActorSub, WorkRef, BackplaneOverride string
	ActiveOnly, JSONOut                                                      bool
	Limit                                                                    int
}

func runRecent(cmd *cobra.Command, opts recentOptions) error {
	if opts.Limit < 1 || opts.Limit > 1000 {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("--limit must be in 1..1000"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if err != nil {
		return err
	}
	params := &api.RecentBroadcastEventsApiV1BroadcastRecentGetParams{Limit: &opts.Limit}
	setRecentParams(params, opts)
	response, err := recentBroadcast(cmd.Context(), client, params)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if response == nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("backplane returned 2xx but no JSON history body"), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), response)
	}
	return printRecent(cmd.OutOrStdout(), response)
}

func setRecentParams(params *api.RecentBroadcastEventsApiV1BroadcastRecentGetParams, opts recentOptions) {
	if opts.Cursor != "" {
		params.Cursor = &opts.Cursor
	}
	if opts.OpClass != "" {
		params.OpClass = &opts.OpClass
	}
	if opts.Principal != "" {
		params.Principal = &opts.Principal
	}
	if opts.Target != "" {
		params.Target = &opts.Target
	}
	if opts.ActorSub != "" {
		params.ActorSub = &opts.ActorSub
	}
	if opts.WorkRef != "" {
		params.WorkRef = &opts.WorkRef
	}
	if opts.ActiveOnly {
		params.ActiveOnly = &opts.ActiveOnly
	}
}

func recentBroadcast(ctx context.Context, client *api.AuthedClient, params *api.RecentBroadcastEventsApiV1BroadcastRecentGetParams) (*map[string]interface{}, error) {
	resp, err := client.RecentBroadcastEventsApiV1BroadcastRecentGetWithResponse(ctx, params)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if err := client.Refresh(ctx); err != nil {
			return nil, err
		}
		resp, err = client.RecentBroadcastEventsApiV1BroadcastRecentGetWithResponse(ctx, params)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

func printRecent(w io.Writer, response *map[string]interface{}) error {
	events, _ := (*response)["events"].([]interface{})
	if len(events) == 0 {
		_, err := fmt.Fprintln(w, "(no broadcast events in this page)")
		return err
	}
	for _, event := range events {
		if err := output.PrintJSON(w, event); err != nil {
			return err
		}
	}
	return nil
}

func newAnnounceCmd() *cobra.Command {
	var opts announceOptions
	cmd := &cobra.Command{
		Use: "announce <activity>", Short: "Publish a governed broadcast announcement", Args: cobra.ExactArgs(1), SilenceUsage: true, SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error { opts.Activity = args[0]; return runAnnounce(cmd, opts) },
	}
	cmd.Flags().StringVar(&opts.Target, "target", "", "target name")
	cmd.Flags().StringVar(&opts.Scope, "scope", "", "announcement scope")
	cmd.Flags().StringVar(&opts.Phase, "phase", "update", "start, update, or completion")
	cmd.Flags().StringSliceVar(&opts.Targets, "targets", nil, "target names")
	cmd.Flags().StringVar(&opts.PlannedOpClass, "planned-op-class", "", "declared operation class")
	cmd.Flags().IntVar(&opts.TTLMinutes, "ttl-minutes", 0, "claim TTL in minutes (1-1440)")
	cmd.Flags().StringVar(&opts.WorkRef, "work-ref", "", "external work reference")
	cmd.Flags().StringVar(&opts.RunID, "run-id", "", "agent run UUID")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "emit JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "", "backplane URL")
	return cmd
}

type announceOptions struct {
	Activity, Target, Scope, Phase, PlannedOpClass, WorkRef, RunID, BackplaneOverride string
	Targets                                                                           []string
	TTLMinutes                                                                        int
	JSONOut                                                                           bool
}

func runAnnounce(cmd *cobra.Command, opts announceOptions) error {
	if opts.Phase != "start" && opts.Phase != "update" && opts.Phase != "completion" {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("--phase must be start, update, or completion"), opts.JSONOut)
	}
	if opts.TTLMinutes < 0 || opts.TTLMinutes > 1440 {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("--ttl-minutes must be in 1..1440 when set"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if err != nil {
		return err
	}
	body := api.BroadcastAnnounceRequest{Activity: opts.Activity, Targets: &opts.Targets}
	phase := api.BroadcastAnnounceRequestPhase(opts.Phase)
	body.Phase = &phase
	if opts.Target != "" {
		body.Target = &opts.Target
	}
	if opts.Scope != "" {
		body.Scope = &opts.Scope
	}
	if opts.WorkRef != "" {
		body.WorkRef = &opts.WorkRef
	}
	if opts.PlannedOpClass != "" {
		planned := api.BroadcastAnnounceRequestPlannedOpClass(opts.PlannedOpClass)
		body.PlannedOpClass = &planned
	}
	if opts.TTLMinutes != 0 {
		body.TtlMinutes = &opts.TTLMinutes
	}
	if opts.RunID != "" {
		runID, err := uuid.Parse(opts.RunID)
		if err != nil {
			return output.RenderError(
				cmd.ErrOrStderr(),
				output.Unexpected("--run-id must be a UUID"),
				opts.JSONOut,
			)
		}
		body.RunId = &runID
	}
	response, err := announceBroadcast(cmd.Context(), client, body)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if response == nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected("backplane returned 2xx but no announcement acknowledgement"), opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), response)
	}
	_, err = fmt.Fprintf(cmd.OutOrStdout(), "announcement published: event_id=%s cursor=%s\n", response.EventId, response.Cursor)
	return err
}

func announceBroadcast(ctx context.Context, client *api.AuthedClient, body api.BroadcastAnnounceRequest) (*api.BroadcastAnnounceResponse, error) {
	params := &api.AnnounceBroadcastApiV1BroadcastAnnouncePostParams{}
	resp, err := client.AnnounceBroadcastApiV1BroadcastAnnouncePostWithResponse(ctx, params, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() == http.StatusUnauthorized {
		if err := client.Refresh(ctx); err != nil {
			return nil, err
		}
		resp, err = client.AnnounceBroadcastApiV1BroadcastAnnouncePostWithResponse(ctx, params, body)
		if err != nil {
			return nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON201, nil
}

func newWatchCmd() *cobra.Command {
	var opts watchOptions
	cmd := &cobra.Command{Use: "watch", Short: "Tail the tenant broadcast SSE feed", Args: cobra.NoArgs, SilenceUsage: true, SilenceErrors: true, RunE: func(cmd *cobra.Command, _ []string) error { return runBroadcastWatch(cmd, opts) }}
	cmd.Flags().StringVar(&opts.OpClass, "op-class", "", "exact op class filter")
	cmd.Flags().StringVar(&opts.Principal, "principal", "", "exact principal filter")
	cmd.Flags().StringVar(&opts.Target, "target", "", "exact target filter")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "emit each event as JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "", "backplane URL")
	return cmd
}

type watchOptions struct {
	OpClass, Principal, Target, BackplaneOverride string
	JSONOut                                       bool
}

func runBroadcastWatch(cmd *cobra.Command, opts watchOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, err := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if err != nil {
		return err
	}
	feedURL, err := buildBroadcastFeedURL(backplaneURL, opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	req, err := http.NewRequestWithContext(cmd.Context(), http.MethodGet, feedURL, nil)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(fmt.Sprintf("build feed request: %v", err)), opts.JSONOut)
	}
	req.Header.Set("Authorization", "Bearer "+client.AccessToken())
	req.Header.Set("Accept", "text/event-stream")
	resp, err := client.HTTPClient().Do(req)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)), opts.JSONOut)
	}
	if resp.StatusCode == http.StatusUnauthorized {
		resp.Body.Close() //nolint:errcheck
		if err := client.Refresh(cmd.Context()); err != nil {
			return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
		}
		req.Header.Set("Authorization", "Bearer "+client.AccessToken())
		resp, err = client.HTTPClient().Do(req)
		if err != nil {
			return output.RenderError(
				cmd.ErrOrStderr(),
				output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
				opts.JSONOut,
			)
		}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		return routeRequestError(cmd, backplaneURL, &httpResponseError{statusCode: resp.StatusCode, body: body}, opts.JSONOut)
	}
	return renderBroadcastSSE(resp.Body, cmd.OutOrStdout(), opts.JSONOut)
}

func buildBroadcastFeedURL(backplaneURL string, opts watchOptions) (string, error) {
	u, err := url.Parse(backplaneURL)
	if err != nil {
		return "", fmt.Errorf("build feed URL: %w", err)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/v1/feed"
	q := u.Query()
	if opts.OpClass != "" {
		q.Set("op_class", opts.OpClass)
	}
	if opts.Principal != "" {
		q.Set("principal", opts.Principal)
	}
	if opts.Target != "" {
		q.Set("target", opts.Target)
	}
	u.RawQuery = q.Encode()
	return u.String(), nil
}

func renderBroadcastSSE(r io.Reader, w io.Writer, jsonOut bool) error {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var data []string
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if len(data) > 0 {
				raw := strings.Join(data, "\n")
				if jsonOut {
					if _, err := fmt.Fprintln(w, raw); err != nil {
						return err
					}
				} else {
					var event map[string]interface{}
					if err := json.Unmarshal([]byte(raw), &event); err != nil {
						return err
					}
					if _, err := fmt.Fprintf(w, "%v\n", event); err != nil {
						return err
					}
				}
				data = nil
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	return scanner.Err()
}
