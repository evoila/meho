// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package eventsource

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns `meho event-source list`.
func newListCmd() *cobra.Command {
	var (
		statusFlag        string
		limit             int
		cursor            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "list",
		Short:         "List event sources in your tenant",
		Long:          "list calls GET /api/v1/event-sources, keyset-paginated by name, optionally filtered by status.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			q := url.Values{}
			if cmd.Flags().Changed("status") {
				q.Set("status", statusFlag)
			}
			if cmd.Flags().Changed("limit") {
				q.Set("limit", fmt.Sprintf("%d", limit))
			}
			if cursor != "" {
				q.Set("cursor", cursor)
			}
			path := "/api/v1/event-sources"
			if len(q) > 0 {
				path += "?" + q.Encode()
			}
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodGet, path, nil)
			if err != nil {
				return renderRequestErr(cmd, backplaneURL, err, jsonOut)
			}
			var envelope EventSourceListResponse
			if err := json.Unmarshal(raw, &envelope); err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("decode event source list: %v", err)), jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), envelope.Items)
			}
			printTable(cmd.OutOrStdout(), envelope.Items)
			return nil
		},
	}
	cmd.Flags().StringVar(&statusFlag, "status", "", "filter by status: active | paused")
	cmd.Flags().IntVar(&limit, "limit", 0, "max sources per page (1..500, server default 100)")
	cmd.Flags().StringVar(&cursor, "cursor", "", "keyset cursor (the last name from the previous page)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit machine-readable JSON instead of the table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to `meho login`'s)")
	return cmd
}

func printTable(w io.Writer, items []EventSourceSummary) {
	if len(items) == 0 {
		fmt.Fprintln(w, "no event sources registered in this tenant")
		return
	}
	fmt.Fprintf(w, "%-24s %-18s %-16s %s\n", "SLUG", "KIND", "AUTH", "STATUS")
	for _, s := range items {
		fmt.Fprintf(w, "%-24s %-18s %-16s %s\n",
			truncate(s.Slug, 24), truncate(s.Kind, 18), truncate(s.AuthStrategy, 16), s.Status)
	}
}
