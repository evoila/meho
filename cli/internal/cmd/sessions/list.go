// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"context"
	"fmt"
	"net/http"
	"text/tabwriter"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

type listOptions struct {
	Project           string
	State             string
	Since             string
	Until             string
	Limit             int
	All               bool
	JSONOut           bool
	BackplaneOverride string
}

func newListCmd() *cobra.Command {
	var opts listOptions
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List the stored agent sessions of your tenant (GET /api/v1/sessions)",
		Long: "list shows the stored sessions of your tenant, newest upload first. " +
			"Without --all it shows one page and says when more pages exist. " +
			"With --since and --until it shows only the sessions of that time: " +
			"the last upload is at or after --since, and the start is before --until. " +
			"Needs the operator role.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.Project, "project", "", "only sessions of this project")
	cmd.Flags().StringVar(&opts.State, "state", "", "only sessions in this state: open, ended or unfinished")
	cmd.Flags().StringVar(&opts.Since, "since", "",
		"only sessions with an upload at or after this time (RFC 3339 with a time zone, for example 2026-11-02T00:00:00Z)")
	cmd.Flags().StringVar(&opts.Until, "until", "",
		"only sessions that started before this time (RFC 3339 with a time zone)")
	cmd.Flags().IntVar(&opts.Limit, "limit", 50, "sessions per page (1 to 200)")
	cmd.Flags().BoolVar(&opts.All, "all", false, "follow every page to the end")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false, "print the sessions as JSON")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by `meho login`)")
	return cmd
}

// listResult is what --json prints: every session read, and the cursor of
// the next page when one exists (null when the list is complete).
type listResult struct {
	Items      []api.SessionSummary `json:"items"`
	NextCursor *string              `json:"next_cursor"`
}

func runList(cmd *cobra.Command, opts listOptions) error {
	switch opts.State {
	case "", "open", "ended", "unfinished":
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage("--state must be open, ended or unfinished"), opts.JSONOut)
	}
	if opts.Limit < 1 || opts.Limit > 200 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Usage("--limit must be between 1 and 200"), opts.JSONOut)
	}
	window, err := parseWindow(opts.Since, opts.Until)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Usage(err.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result := listResult{Items: []api.SessionSummary{}}
	var cursor *string
	for {
		params := buildListParams(opts, window, cursor)
		resp, err := retryOn401(cmd.Context(), authed,
			func(ctx context.Context) (*api.ListAgentSessionsApiV1SessionsGetResponse, error) {
				return authed.ListAgentSessionsApiV1SessionsGetWithResponse(ctx, params)
			},
			func(r *api.ListAgentSessionsApiV1SessionsGetResponse) int { return r.StatusCode() },
		)
		if err != nil {
			return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
		}
		if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
			return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
		}
		result.Items = append(result.Items, resp.JSON200.Items...)
		result.NextCursor = resp.JSON200.NextCursor
		if !opts.All || result.NextCursor == nil {
			break
		}
		cursor = result.NextCursor
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printList(cmd, result)
	return nil
}

// timeWindow holds the parsed --since and --until; nil means "not set".
type timeWindow struct {
	Since *time.Time
	Until *time.Time
}

// parseWindow reads --since and --until as RFC 3339 times. RFC 3339 always
// has a time zone, so a time without one is refused here, like the server does.
func parseWindow(since, until string) (timeWindow, error) {
	var w timeWindow
	for _, f := range []struct {
		name, value string
		dst         **time.Time
	}{{"--since", since, &w.Since}, {"--until", until, &w.Until}} {
		if f.value == "" {
			continue
		}
		t, err := time.Parse(time.RFC3339, f.value)
		if err != nil {
			return w, fmt.Errorf("%s must be an RFC 3339 time with a time zone, for example 2026-11-02T00:00:00Z", f.name)
		}
		*f.dst = &t
	}
	if w.Since != nil && w.Until != nil && w.Since.After(*w.Until) {
		return w, fmt.Errorf("--since must not be after --until")
	}
	return w, nil
}

func buildListParams(opts listOptions, window timeWindow, cursor *string) *api.ListAgentSessionsApiV1SessionsGetParams {
	limit := opts.Limit
	params := &api.ListAgentSessionsApiV1SessionsGetParams{
		Limit: &limit, Cursor: cursor, Since: window.Since, Until: window.Until,
	}
	if opts.Project != "" {
		project := opts.Project
		params.Project = &project
	}
	if opts.State != "" {
		state := api.ListAgentSessionsApiV1SessionsGetParamsState(opts.State)
		params.State = &state
	}
	return params
}

func printList(cmd *cobra.Command, result listResult) {
	out := cmd.OutOrStdout()
	if len(result.Items) == 0 {
		fmt.Fprintln(out, "no sessions")
	} else {
		tw := tabwriter.NewWriter(out, 0, 0, 2, ' ', 0)
		fmt.Fprintln(tw, "SESSION\tPROJECT\tAPP\tSTATE\tUPLOADS\tLAST UPLOAD\tBY")
		for _, s := range result.Items {
			fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%d\t%s\t%s\n",
				s.SessionId, s.Project, s.App, s.State, s.UploadCount,
				s.LastSeenAt.UTC().Format(time.RFC3339), s.UploadedBySub)
		}
		_ = tw.Flush()
	}
	if result.NextCursor != nil {
		fmt.Fprintf(out, "more sessions exist; run again with --all to list every page\n")
	}
}
