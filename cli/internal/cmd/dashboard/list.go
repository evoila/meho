// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"context"
	"fmt"
	"io"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho dashboard list` command.
//
//	meho dashboard list [--tenant T] [--limit N] [--offset N]
//	                    [--json] [--backplane <url>]
//
// Role: operator. Operator role is scoped to its own tenant; --tenant is a
// platform_admin-only filter (the backend returns 403 otherwise).
func newListCmd() *cobra.Command {
	var (
		tenant            string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List dashboards in your tenant",
		Long: "list calls GET /api/v1/checks/dashboards and renders the " +
			"dashboards in the operator's tenant, newest-first. Each row " +
			"carries the rolled-up state (worst-of its member sensors, " +
			"evaluated on read) and the member_count, so the list answers " +
			"\"is everything OK?\" per dashboard without a detail fetch. " +
			"--tenant is a platform_admin-only filter that targets another " +
			"tenant; operator role calling with --tenant lands as 403 " +
			"insufficient_role. --limit caps the page size (1..500, server " +
			"default 100). --offset advances the page window (default 0). " +
			"--json emits the raw ListResponse envelope.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Tenant:            tenant,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (platform_admin only; operator role is locked to its own tenant)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max dashboards per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Tenant            string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if opts.Limit < 0 || int64(opts.Limit) > maxDashboardListRows {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and %d; got %d",
				maxDashboardListRows, opts.Limit)),
			opts.JSONOut)
	}
	if opts.Offset < 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--offset must be non-negative; got %d", opts.Offset)),
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
	resp, err := getList(cmd.Context(), backplaneURL, opts, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil (the
	// generated parser only populates it when Content-Type is JSON). Without
	// the guard a malformed 200 would print "no dashboards in this tenant" as
	// if the tenant genuinely had zero — actively misleading.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a dashboard list payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listQueryParams maps the CLI flags onto the generated query-param shape.
// --tenant is a UUID pointer; --limit / --offset only send when non-zero so
// the backend's default page-size kicks in when the operator omits the flag.
func listQueryParams(
	opts listOptions,
	tenantFilter *openapi_types.UUID,
) *api.ListDashboardsApiV1ChecksDashboardsGetParams {
	params := &api.ListDashboardsApiV1ChecksDashboardsGetParams{}
	if tenantFilter != nil {
		params.TenantFilter = tenantFilter
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Offset > 0 {
		o := opts.Offset
		params.Offset = &o
	}
	return params
}

// getList calls GET /api/v1/checks/dashboards via the generated typed client.
func getList(
	ctx context.Context,
	backplaneURL string,
	opts listOptions,
	tenantFilter *openapi_types.UUID,
) (*api.ListDashboardsApiV1ChecksDashboardsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts, tenantFilter)
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.ListDashboardsApiV1ChecksDashboardsGetResponse, error) {
			return authed.ListDashboardsApiV1ChecksDashboardsGetWithResponse(ctx, params)
		},
		func(r *api.ListDashboardsApiV1ChecksDashboardsGetResponse) int { return r.StatusCode() },
	)
}

// printListTable renders the dashboards as a compact table:
// ID, NAME, STATE, MEMBERS, UPDATED_AT.
func printListTable(w io.Writer, r *api.DashboardListResponse) {
	if r == nil || len(r.Dashboards) == 0 {
		fmt.Fprintln(w, "no dashboards in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-24s %-10s %-8s %s\n",
		"ID", "NAME", "STATE", "MEMBERS", "UPDATED_AT")
	for _, d := range r.Dashboards {
		// Name carries operator-persisted free-form text; sanitize so
		// terminal control chars / ANSI escapes can't affect the operator's
		// terminal. The --json path (runList) serialises the raw value
		// unchanged.
		fmt.Fprintf(w, "%-36s %-24s %-10s %-8d %s\n",
			d.Id.String(), sanitizeCell(d.Name), string(d.State),
			d.MemberCount, formatTime(&d.UpdatedAt))
	}
}
