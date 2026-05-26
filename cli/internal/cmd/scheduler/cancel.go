// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"context"
	"fmt"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCancelCmd returns the `meho scheduler cancel` command.
//
//	meho scheduler cancel <trigger_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. Transitions a trigger to terminal
// status='cancelled'. Idempotent on an already-cancelled trigger;
// rejects a terminal-fired one-off with 409.
func newCancelCmd() *cobra.Command {
	var (
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "cancel <trigger_id>",
		Short: "Cancel one scheduled trigger by id (tenant_admin)",
		Long: "cancel calls DELETE /api/v1/scheduler/triggers/{id} to " +
			"transition a trigger to terminal status='cancelled'. The " +
			"row is retained for audit but never fires again. " +
			"Tenant_admin only — operator-role JWT lands as 403 " +
			"insufficient_role.\n\n" +
			"Idempotent on an already-cancelled trigger. A terminal " +
			"one-off in status='fired' is **not** cancellable (returns " +
			"409 trigger_already_fired). A cross-tenant / absent id " +
			"returns 404 trigger_not_found (existence is not leaked " +
			"across tenants).\n\n" +
			"--tenant targets another tenant (tenant_admin cross-tenant " +
			"cancel).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCancel(cmd, cancelOptions{
				TriggerID:         args[0],
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (tenant_admin cross-tenant cancel)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a structured JSON result instead of plain text")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type cancelOptions struct {
	TriggerID         string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

// buildCancelPath assembles the DELETE /api/v1/scheduler/triggers/{id}
// path with the optional tenant_filter query.
func buildCancelPath(opts cancelOptions) string {
	path := "/api/v1/scheduler/triggers/" + url.PathEscape(opts.TriggerID)
	if opts.Tenant != "" {
		q := url.Values{}
		q.Set("tenant_filter", opts.Tenant)
		path = path + "?" + q.Encode()
	}
	return path
}

func runCancel(cmd *cobra.Command, opts cancelOptions) error {
	if opts.TriggerID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("cancel requires a non-empty <trigger_id> argument"), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	if err := deleteCancel(cmd.Context(), backplaneURL, opts); err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(),
			map[string]any{"trigger_id": opts.TriggerID, "cancelled": true})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "cancelled trigger %s\n", opts.TriggerID)
	return nil
}

func deleteCancel(ctx context.Context, backplaneURL string, opts cancelOptions) error {
	_, err := doAuthedRequest(ctx, backplaneURL, "DELETE", buildCancelPath(opts), nil)
	return err
}
