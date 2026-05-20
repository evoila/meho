// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package broadcast

import (
	"context"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

func newOverridesRemoveCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "remove <override-id>",
		Short: "Delete a broadcast-detail override rule by id",
		Long: "remove calls DELETE /api/v1/broadcast/overrides/{id}. Silent " +
			"on success (mirrors `meho` UX convention). A 404 means either " +
			"the id doesn't exist or it belongs to another tenant -- the " +
			"backend deliberately conflates the two so existence is not " +
			"leaked across tenant boundaries.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runOverridesRemove(cmd, overridesRemoveOptions{
				OverrideID:        args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit JSON error envelope on failure (success is still silent)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type overridesRemoveOptions struct {
	OverrideID        string
	JSONOut           bool
	BackplaneOverride string
}

func runOverridesRemove(cmd *cobra.Command, opts overridesRemoveOptions) error {
	if opts.OverrideID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("remove requires a non-empty <override-id> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	if err := deleteOverride(cmd.Context(), backplaneURL, opts.OverrideID); err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Silent on success -- mirrors `meho` UX convention for DELETE.
	return nil
}

// buildRemovePath assembles the DELETE path with the override id
// path-escaped. Exposed for unit tests.
func buildRemovePath(overrideID string) string {
	return "/api/v1/broadcast/overrides/" + pathEscape(overrideID)
}

func deleteOverride(ctx context.Context, backplaneURL, overrideID string) error {
	_, err := doAuthedRequest(ctx, backplaneURL, "DELETE", buildRemovePath(overrideID), nil)
	return err
}
