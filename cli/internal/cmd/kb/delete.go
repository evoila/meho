// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newDeleteCmd returns the `meho kb delete` command.
//
// CLI shape (per issue #418):
//
//	meho kb delete <slug> [--confirm] [--json] [--backplane <url>]
//
// Role: tenant_admin. Operator-role JWT lands as 403
// insufficient_role.
//
// Default behaviour prompts for a y/N confirmation on stdin;
// --confirm skips the prompt for scripted use (CI pipelines, etc.).
// Delete is **idempotent** at the substrate: a delete against an
// absent slug returns 204 (not 404), matching the kb route
// contract. Output mentions idempotency so operators rerunning the
// command after a previous successful run don't mistake the no-op
// for an error.
//
// Exit codes:
//   - 0   delete succeeded (204) or operator declined the prompt
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (a 404 here would signal contract
//     drift since delete is idempotent server-side)
//   - 5   insufficient_role
func newDeleteCmd() *cobra.Command {
	var (
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <slug>",
		Short: "Delete one kb entry by slug (tenant_admin)",
		Long: "delete calls DELETE /api/v1/kb/{slug}. Tenant_admin " +
			"only — operator-role JWT lands as 403 insufficient_role. " +
			"Delete is idempotent server-side: a delete against an " +
			"absent slug returns 204 (the conflation prevents " +
			"enumerating other tenants via status-code differential).\n\n" +
			"Without --confirm, the verb prompts on stdin for a y/N " +
			"confirmation; --confirm skips the prompt for scripted use " +
			"(CI pipelines, etc.). Declining the prompt exits 0 without " +
			"calling the backend.",
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

// deleteResult is the structure printed in --json mode. Kept small
// (slug + status) so operators piping into jq get a stable envelope
// regardless of whether the row existed server-side (the substrate
// doesn't surface that distinction back to the CLI).
type deleteResult struct {
	Slug   string `json:"slug"`
	Status string `json:"status"`
}

func runDelete(cmd *cobra.Command, opts deleteOptions) error {
	if opts.Slug == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("delete requires a non-empty <slug> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	if !opts.Confirm {
		prompt := fmt.Sprintf(
			"Delete kb entry %q — idempotent (no-op if already absent). Continue?",
			opts.Slug,
		)
		if !confirmPrompt(cmd, prompt) {
			result := deleteResult{Slug: opts.Slug, Status: "declined"}
			if opts.JSONOut {
				return output.PrintJSON(cmd.OutOrStdout(), result)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "declined: kb entry %q not deleted\n", opts.Slug)
			return nil
		}
	}
	if err := callDelete(cmd.Context(), backplaneURL, opts.Slug); err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result := deleteResult{Slug: opts.Slug, Status: "deleted"}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted kb entry %q\n", opts.Slug)
	return nil
}

// buildDeletePath assembles the DELETE path. Exposed for unit tests
// so URL encoding of slugs with dots stays covered.
func buildDeletePath(slug string) string {
	return "/api/v1/kb/" + pathEscape(slug)
}

func callDelete(ctx context.Context, backplaneURL, slug string) error {
	// doAuthedRequest returns (nil, nil) on a 204; the kb delete
	// route emits an empty 204 body whether or not the row existed
	// server-side. We discard the (nil) response — the success
	// signal is the absence of an error.
	_, err := doAuthedRequest(ctx, backplaneURL, "DELETE", buildDeletePath(slug), nil)
	return err
}
