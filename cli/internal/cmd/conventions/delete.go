// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package conventions

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDeleteCmd returns the `meho conventions delete` command.
//
//	meho conventions delete <slug> [--confirm] [--json] [--backplane <url>]
//
// Role: tenant_admin. Without --confirm the verb prompts for a y/N
// confirmation on stdin; --confirm skips the prompt for scripted use.
// A 404 (`convention_not_found`) covers absence / cross-tenant.
//
// The substrate writes a DELETE event into the convention's history
// trail (a `body_after` containing the final body), so a deleted-then-
// recreated slug retains an audit trail.
//
// Exit codes:
//   - 0   delete succeeded (204) or operator declined the prompt
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response
//   - 5   insufficient_role
func newDeleteCmd() *cobra.Command {
	var (
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <slug>",
		Short: "Delete one convention by slug (tenant_admin)",
		Long: "delete calls DELETE /api/v1/conventions/{slug}. " +
			"Tenant_admin only. Without --confirm the verb prompts on " +
			"stdin for a y/N confirmation; --confirm skips the prompt for " +
			"scripted use (CI pipelines, etc.). Declining the prompt " +
			"exits 0 without calling the backend.\n\n" +
			"The substrate writes a DELETE event into the convention's " +
			"history trail so audit forensics retain the final body — a " +
			"deleted-then-recreated slug keeps a complete edit history.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDelete(cmd, deleteOptions{
				Slug:              args[0],
				Confirm:           confirm,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&confirm, "confirm", false,
		"skip the stdin confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a machine-readable success envelope instead of the human line")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type deleteOptions struct {
	Slug              string
	Confirm           bool
	JSONOut           bool
	BackplaneOverride string
}

// deleteResult is the structure printed in --json mode.
type deleteResult struct {
	Slug   string `json:"slug"`
	Status string `json:"status"`
}

func runDelete(cmd *cobra.Command, opts deleteOptions) error {
	if opts.Slug == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("delete requires a non-empty <slug> argument"), opts.JSONOut)
	}
	// Confirm BEFORE resolving the backplane so an operator who
	// declines (or hits EOF on a piped /dev/null) exits 0 with the
	// declined envelope regardless of whether `meho login` has been
	// run. Mirrors the kb / agent delete shape.
	if !opts.Confirm {
		prompt := fmt.Sprintf("Delete convention %q. Continue?", opts.Slug)
		if !confirmPrompt(cmd, prompt) {
			result := deleteResult{Slug: opts.Slug, Status: "declined"}
			if opts.JSONOut {
				return output.PrintJSON(cmd.OutOrStdout(), result)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "declined: convention %q not deleted\n", opts.Slug)
			return nil
		}
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	if err := callDelete(cmd.Context(), backplaneURL, opts.Slug); err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result := deleteResult{Slug: opts.Slug, Status: "deleted"}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted convention %q\n", opts.Slug)
	return nil
}

func callDelete(ctx context.Context, backplaneURL, slug string) error {
	// doAuthedRequest returns (nil, nil) on a 204; the success signal
	// is the absence of an error.
	_, err := doAuthedRequest(ctx, backplaneURL, "DELETE", buildShowPath(slug), nil)
	return err
}
