// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package eventsource

import (
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// deleteResult is the --json success/decline envelope.
type deleteResult struct {
	Slug   string `json:"slug"`
	Status string `json:"status"`
}

// newDeleteCmd returns `meho event-source delete <slug>`.
func newDeleteCmd() *cobra.Command {
	var (
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:           "delete <slug>",
		Short:         "Soft-delete one event source by slug (tenant_admin)",
		Long:          "delete calls DELETE /api/v1/event-sources/{slug}. Without --confirm it prompts for a y/N on stdin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			slug := args[0]
			// Confirm before resolving the backplane so a decline exits 0
			// regardless of login state.
			if !confirm && !confirmPrompt(cmd, fmt.Sprintf("Delete event source %q. Continue?", slug)) {
				result := deleteResult{Slug: slug, Status: "declined"}
				if jsonOut {
					return output.PrintJSON(cmd.OutOrStdout(), result)
				}
				fmt.Fprintf(cmd.OutOrStdout(), "declined: event source %q not deleted\n", slug)
				return nil
			}
			backplaneURL, err := backplane.Resolve(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
			}
			path := "/api/v1/event-sources/" + pathEscape(slug)
			if _, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodDelete, path, nil); err != nil {
				return renderRequestErr(cmd, backplaneURL, err, jsonOut)
			}
			result := deleteResult{Slug: slug, Status: "deleted"}
			if jsonOut {
				return output.PrintJSON(cmd.OutOrStdout(), result)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "deleted event source %q\n", slug)
			return nil
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false, "skip the stdin confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a machine-readable envelope instead of the human line")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "", "backplane URL (defaults to `meho login`'s)")
	return cmd
}
