// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"context"
	"fmt"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDeleteCmd returns the `meho dashboard delete` command.
//
//	meho dashboard delete <dashboard_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. Hard-deletes the dashboard row (its member links go
// with it; the member Sensors themselves are untouched). A cross-tenant /
// absent id returns 404 dashboard_not_found.
func newDeleteCmd() *cobra.Command {
	var (
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <dashboard_id>",
		Short: "Delete one dashboard by id (tenant_admin)",
		Long: "delete calls DELETE /api/v1/checks/dashboards/{id} to " +
			"hard-delete a dashboard (its member links are removed; the member " +
			"Sensors themselves are untouched). Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"A cross-tenant / absent id returns 404 dashboard_not_found " +
			"(existence is not leaked across tenants).\n\n" +
			"--tenant targets another tenant (platform_admin cross-tenant " +
			"delete).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDelete(cmd, deleteOptions{
				DashboardID:       args[0],
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant delete)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a structured JSON result instead of plain text")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type deleteOptions struct {
	DashboardID       string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runDelete(cmd *cobra.Command, opts deleteOptions) error {
	if opts.DashboardID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("delete requires a non-empty <dashboard_id> argument"), opts.JSONOut)
	}
	var dashboardID openapi_types.UUID
	if err := dashboardID.UnmarshalText([]byte(opts.DashboardID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("dashboard-id is not a valid UUID: %v", err)),
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
	resp, err := deleteDashboard(cmd.Context(), backplaneURL, dashboardID, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Delete returns 204 No Content on success (the generated envelope has
	// no typed JSON field — only Body and HTTPResponse). Treat any 2xx as
	// success; everything else routes through renderHTTPStatus.
	status := resp.StatusCode()
	if status < 200 || status >= 300 {
		return renderHTTPStatus(cmd, backplaneURL, status, resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(),
			map[string]any{"dashboard_id": opts.DashboardID, "deleted": true})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted dashboard %s\n", opts.DashboardID)
	return nil
}

// deleteDashboard calls DELETE /api/v1/checks/dashboards/{id} via the
// generated typed client.
func deleteDashboard(
	ctx context.Context,
	backplaneURL string,
	dashboardID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
) (*api.DeleteDashboardApiV1ChecksDashboardsDashboardIdDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.DeleteDashboardApiV1ChecksDashboardsDashboardIdDeleteParams{}
	if tenantFilter != nil {
		params.TenantFilter = tenantFilter
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.DeleteDashboardApiV1ChecksDashboardsDashboardIdDeleteResponse, error) {
			return authed.DeleteDashboardApiV1ChecksDashboardsDashboardIdDeleteWithResponse(ctx, dashboardID, params)
		},
		func(r *api.DeleteDashboardApiV1ChecksDashboardsDashboardIdDeleteResponse) int { return r.StatusCode() },
	)
}
