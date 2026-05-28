// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"context"
	"fmt"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
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

func runCancel(cmd *cobra.Command, opts cancelOptions) error {
	if opts.TriggerID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("cancel requires a non-empty <trigger_id> argument"), opts.JSONOut)
	}
	// Parse the path-arg UUID CLI-side so a malformed value surfaces
	// locally rather than as a 422 round-trip; the generated DELETE
	// signature requires `openapi_types.UUID`.
	var triggerID openapi_types.UUID
	if err := triggerID.UnmarshalText([]byte(opts.TriggerID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("trigger-id is not a valid UUID: %v", err)),
			opts.JSONOut)
	}
	var tenantFilter *openapi_types.UUID
	if opts.Tenant != "" {
		var parsed openapi_types.UUID
		if err := parsed.UnmarshalText([]byte(opts.Tenant)); err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("--tenant is not a valid UUID: %v", err)),
				opts.JSONOut)
		}
		tenantFilter = &parsed
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := deleteCancel(cmd.Context(), backplaneURL, triggerID, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Cancel returns 204 No Content on success (the generated
	// envelope has no JSON200/JSON204 typed field — only `Body`
	// and `HTTPResponse`). Treat any 2xx as success; everything
	// else routes through renderHTTPStatus.
	status := resp.StatusCode()
	if status < 200 || status >= 300 {
		return renderHTTPStatus(cmd, backplaneURL, status, resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(),
			map[string]any{"trigger_id": opts.TriggerID, "cancelled": true})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "cancelled trigger %s\n", opts.TriggerID)
	return nil
}

// deleteCancel calls DELETE /api/v1/scheduler/triggers/{id} via the
// generated typed client. The 401-refresh-retry loop runs through
// retryOn401. The authed-client construction is hoisted out of the
// retry loop so a credential failure routes directly to
// renderRequestError rather than getting swallowed by the retry
// shape (per the T11 #1285 iter-2 lesson).
func deleteCancel(
	ctx context.Context,
	backplaneURL string,
	triggerID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
) (*api.CancelTriggerApiV1SchedulerTriggersTriggerIdDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.CancelTriggerApiV1SchedulerTriggersTriggerIdDeleteParams{}
	if tenantFilter != nil {
		params.TenantFilter = tenantFilter
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.CancelTriggerApiV1SchedulerTriggersTriggerIdDeleteResponse, error) {
			return authed.CancelTriggerApiV1SchedulerTriggersTriggerIdDeleteWithResponse(ctx, triggerID, params)
		},
		func(r *api.CancelTriggerApiV1SchedulerTriggersTriggerIdDeleteResponse) int {
			return r.StatusCode()
		},
	)
}
