// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

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

// newListCmd returns the `meho sensor list` command.
//
//	meho sensor list [--status S] [--cadence-kind K] [--tenant T]
//	                 [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Operator role is scoped to its own tenant; --tenant is a
// platform_admin-only filter (the backend returns 403 otherwise).
func newListCmd() *cobra.Command {
	var (
		status            string
		cadenceKind       string
		tenant            string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List sensors in your tenant",
		Long: "list calls GET /api/v1/sensors and renders the sensors in " +
			"the operator's tenant, newest-first. Each row carries its " +
			"latest-result projection (last_state), so the list is also the " +
			"status view. --status narrows to active|paused; --cadence-kind " +
			"narrows to interval|cron. --tenant is a platform_admin-only " +
			"filter that targets another tenant; operator role calling with " +
			"--tenant lands as 403 insufficient_role. --limit caps the page " +
			"size (1..500, server default 100). --offset advances the page " +
			"window (default 0). --json emits the raw ListResponse envelope.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Status:            status,
				CadenceKind:       cadenceKind,
				Tenant:            tenant,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&status, "status", "",
		"filter by sensor status: active | paused")
	cmd.Flags().StringVar(&cadenceKind, "cadence-kind", "",
		"filter by cadence kind: interval | cron")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (platform_admin only; operator role is locked to its own tenant)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max sensors per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Status            string
	CadenceKind       string
	Tenant            string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if opts.Status != "" && !validStatuses[opts.Status] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--status must be one of: active, paused"), opts.JSONOut)
	}
	if opts.CadenceKind != "" && !validCadenceKinds[opts.CadenceKind] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--cadence-kind must be one of: interval, cron"), opts.JSONOut)
	}
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
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
	// the guard a malformed 200 would print "no sensors in this tenant" as
	// if the tenant genuinely had zero — actively misleading.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a sensor list payload",
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
// --status / --cadence-kind are typed enum pointers; --tenant is a UUID
// pointer; --limit / --offset only send when non-zero so the backend's
// default page-size kicks in when the operator omits the flag.
func listQueryParams(
	opts listOptions,
	tenantFilter *openapi_types.UUID,
) *api.ListSensorsApiV1SensorsGetParams {
	params := &api.ListSensorsApiV1SensorsGetParams{}
	if opts.Status != "" {
		s := api.ListSensorsApiV1SensorsGetParamsStatus(opts.Status)
		params.Status = &s
	}
	if opts.CadenceKind != "" {
		k := api.ListSensorsApiV1SensorsGetParamsCadenceKind(opts.CadenceKind)
		params.CadenceKind = &k
	}
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

// getList calls GET /api/v1/sensors via the generated typed client.
func getList(
	ctx context.Context,
	backplaneURL string,
	opts listOptions,
	tenantFilter *openapi_types.UUID,
) (*api.ListSensorsApiV1SensorsGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts, tenantFilter)
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.ListSensorsApiV1SensorsGetResponse, error) {
			return authed.ListSensorsApiV1SensorsGetWithResponse(ctx, params)
		},
		func(r *api.ListSensorsApiV1SensorsGetResponse) int { return r.StatusCode() },
	)
}

// printListTable renders the sensors as a compact table:
// ID, NAME, STATUS, LAST_STATE, CADENCE, NEXT_FIRE_AT, SEVERITY.
func printListTable(w io.Writer, r *api.SensorListResponse) {
	if r == nil || len(r.Sensors) == 0 {
		fmt.Fprintln(w, "no sensors in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-20s %-8s %-10s %-22s %-24s %s\n",
		"ID", "NAME", "STATUS", "LAST_STATE", "CADENCE", "NEXT_FIRE_AT", "SEVERITY")
	for _, s := range r.Sensors {
		cadence := string(s.CadenceKind)
		switch string(s.CadenceKind) {
		case "interval":
			if s.IntervalSeconds != nil {
				cadence = fmt.Sprintf("every %ds", *s.IntervalSeconds)
			}
		case "cron":
			if s.CronExpr != nil {
				cadence = *s.CronExpr
				if s.Timezone != "" && s.Timezone != "UTC" {
					cadence = cadence + " (" + s.Timezone + ")"
				}
			}
		}
		next := "-"
		if s.NextFireAt != nil {
			next = formatTime(s.NextFireAt)
		}
		fmt.Fprintf(w, "%-36s %-20s %-8s %-10s %-22s %-24s %s\n",
			s.Id.String(), s.Name, string(s.Status), string(s.LastState),
			cadence, next, string(s.Severity))
	}
}
