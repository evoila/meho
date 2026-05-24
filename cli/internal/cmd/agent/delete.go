// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"context"
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDeleteCmd returns the `meho agent delete` command.
//
//	meho agent delete <name> [--confirm] [--json] [--backplane <url>]
//
// Role: tenant_admin. Without --confirm the verb prompts for a y/N
// confirmation on stdin; --confirm skips the prompt for scripted use.
// A 404 (`agent_not_found`) covers absence / cross-tenant — never 403,
// so existence is not leaked across tenant boundaries.
func newDeleteCmd() *cobra.Command {
	var (
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <name>",
		Short: "Delete one agent definition by name (tenant_admin)",
		Long: "delete calls DELETE /api/v1/agents/{name}. Tenant_admin " +
			"only. Without --confirm the verb prompts on stdin for a y/N " +
			"confirmation; --confirm skips the prompt for scripted use. " +
			"Declining the prompt exits 0 without calling the backend. A " +
			"404 means the name doesn't exist in your tenant (the route " +
			"conflates cross-tenant probes with genuine absence).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDelete(cmd, deleteOptions{
				Name:              args[0],
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
	Name              string
	Confirm           bool
	JSONOut           bool
	BackplaneOverride string
}

// deleteResult is the structure printed in --json mode.
type deleteResult struct {
	Name   string `json:"name"`
	Status string `json:"status"`
}

func runDelete(cmd *cobra.Command, opts deleteOptions) error {
	if opts.Name == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("delete requires a non-empty <name> argument"), opts.JSONOut)
	}
	// Confirm BEFORE resolving the backplane so an operator who declines
	// (or hits EOF on a piped /dev/null) exits 0 with the declined
	// envelope regardless of whether `meho login` has been run.
	if !opts.Confirm {
		prompt := fmt.Sprintf("Delete agent definition %q. Continue?", opts.Name)
		if !confirmPrompt(cmd, prompt) {
			result := deleteResult{Name: opts.Name, Status: "declined"}
			if opts.JSONOut {
				return output.PrintJSON(cmd.OutOrStdout(), result)
			}
			fmt.Fprintf(cmd.OutOrStdout(), "declined: agent definition %q not deleted\n", opts.Name)
			return nil
		}
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	if err := callDelete(cmd.Context(), backplaneURL, opts.Name); err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result := deleteResult{Name: opts.Name, Status: "deleted"}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted agent definition %q\n", opts.Name)
	return nil
}

func callDelete(ctx context.Context, backplaneURL, name string) error {
	// doAuthedRequest returns (nil, nil) on a 204; the success signal is
	// the absence of an error.
	_, err := doAuthedRequest(ctx, backplaneURL, "DELETE", buildShowPath(name), nil)
	return err
}
