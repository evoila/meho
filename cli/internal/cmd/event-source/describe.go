// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package eventsource

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDescribeCmd returns `meho event-source describe <slug>`.
func newDescribeCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "describe <slug>",
		Short:         "Describe a single event source by slug",
		Long:          "describe calls GET /api/v1/event-sources/{slug}. A slug absent or owned by another tenant returns the same 404.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			path := "/api/v1/event-sources/" + pathEscape(args[0])
			raw, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodGet, path, nil)
			if err != nil {
				return renderRequestErr(cmd, backplaneURL, err, jsonOut)
			}
			var es EventSource
			if err := json.Unmarshal(raw, &es); err != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("decode event source: %v", err)), jsonOut)
			}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), &es)
			}
			printEventSource(cmd.OutOrStdout(), &es)
			return nil
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit machine-readable JSON instead of the summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to `meho login`'s)")
	return cmd
}
