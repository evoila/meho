// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"context"
	"fmt"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newShowCmd returns the `meho dashboard show` command.
//
//	meho dashboard show <dashboard_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: operator. Renders the rolled-up state plus the per-member breakdown
// (each member's raw + effective state) — the CLI twin of
// /ui/checks/{dashboard_id}. A cross-tenant / absent id returns 404
// dashboard_not_found (existence is not leaked across tenants).
func newShowCmd() *cobra.Command {
	var (
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "show <dashboard_id>",
		Short: "Show one dashboard with its member breakdown",
		Long: "show calls GET /api/v1/checks/dashboards/{id} and renders the " +
			"dashboard's rolled-up state (worst-of its member sensors, " +
			"evaluated on read) plus the per-member breakdown: each member's " +
			"raw state, its severity-capped effective contribution, whether a " +
			"failing state is being held pending by the for: window, and the " +
			"member's severity + lifecycle status.\n\n" +
			"Role: operator. A cross-tenant / absent id returns 404 " +
			"dashboard_not_found (existence is not leaked across tenants).\n\n" +
			"--tenant targets another tenant (platform_admin cross-tenant " +
			"read). --json emits the raw DashboardDetail envelope.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runShow(cmd, showOptions{
				DashboardID:       args[0],
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant read)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw DashboardDetail JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type showOptions struct {
	DashboardID       string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runShow(cmd *cobra.Command, opts showOptions) error {
	if opts.DashboardID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("show requires a non-empty <dashboard_id> argument"), opts.JSONOut)
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
	resp, err := getDashboard(cmd.Context(), backplaneURL, dashboardID, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a dashboard detail payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printDashboardSummary(cmd.OutOrStdout(), resp.JSON200)
	printMemberTable(cmd.OutOrStdout(), resp.JSON200.Members)
	return nil
}

// getDashboard calls GET /api/v1/checks/dashboards/{id} via the generated
// typed client.
func getDashboard(
	ctx context.Context,
	backplaneURL string,
	dashboardID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
) (*api.GetDashboardApiV1ChecksDashboardsDashboardIdGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.GetDashboardApiV1ChecksDashboardsDashboardIdGetParams{}
	if tenantFilter != nil {
		params.TenantFilter = tenantFilter
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.GetDashboardApiV1ChecksDashboardsDashboardIdGetResponse, error) {
			return authed.GetDashboardApiV1ChecksDashboardsDashboardIdGetWithResponse(ctx, dashboardID, params)
		},
		func(r *api.GetDashboardApiV1ChecksDashboardsDashboardIdGetResponse) int { return r.StatusCode() },
	)
}
