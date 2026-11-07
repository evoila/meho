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

// newEditCmd returns the `meho dashboard edit` command.
//
//	meho dashboard edit <dashboard_id> [--add-sensor ID ...]
//	  [--remove-sensor ID ...] [--name N] [--notify-email A | --notify-off]
//	  [--notify-min-state S] [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. Only the flags the operator sets are sent. The id
// and every setting the edit does not name stay.
func newEditCmd() *cobra.Command {
	var opts editOptions
	cmd := &cobra.Command{
		Use:   "edit <dashboard_id>",
		Short: "Change members, name or mail settings of one dashboard (tenant_admin)",
		Long: "edit calls PATCH /api/v1/checks/dashboards/{id} to change a " +
			"dashboard in place. Only the flags you set change; the id and " +
			"all other settings stay. Tenant_admin only.\n\n" +
			"--add-sensor and --remove-sensor take a sensor id and can be " +
			"repeated. Adding a sensor that is already a member, or removing " +
			"one that is not, changes nothing, so the same edit is safe to " +
			"run twice. --name gives a new name. --notify-email sets the " +
			"comma-separated mail recipients; --notify-off turns mail off. " +
			"--notify-min-state is degraded or critical.\n\n" +
			"An added sensor that is not in your tenant is refused 422 " +
			"sensor_not_found; a name that is taken 409. A cross-tenant / " +
			"absent id returns 404 dashboard_not_found. The audit log " +
			"records the old and new value of each changed field.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.DashboardID = args[0]
			opts.changed = cmd.Flags().Changed
			return runEdit(cmd, opts)
		},
	}
	f := cmd.Flags()
	f.StringArrayVar(&opts.AddSensorIDs, "add-sensor", nil, "sensor UUID to add (repeatable)")
	f.StringArrayVar(&opts.RemoveSensorIDs, "remove-sensor", nil, "sensor UUID to remove (repeatable)")
	f.StringVar(&opts.Name, "name", "", "new dashboard name (unique per tenant)")
	f.StringVar(&opts.NotifyEmail, "notify-email", "", "new comma-separated mail recipient(s)")
	f.BoolVar(&opts.NotifyOff, "notify-off", false, "turn transition mail off")
	f.StringVar(&opts.NotifyMinState, "notify-min-state", "", "new mail floor: degraded or critical")
	f.StringVar(&opts.Tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant edit)")
	f.BoolVar(&opts.JSONOut, "json", false, "emit raw DashboardDetail JSON instead of the human summary")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type editOptions struct {
	DashboardID       string
	AddSensorIDs      []string
	RemoveSensorIDs   []string
	Name              string
	NotifyEmail       string
	NotifyOff         bool
	NotifyMinState    string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string

	// changed reports whether the operator set a flag. Nil means none.
	changed func(name string) bool
}

func (o editOptions) isSet(name string) bool {
	return o.changed != nil && o.changed(name)
}

func runEdit(cmd *cobra.Command, opts editOptions) error {
	var dashboardID openapi_types.UUID
	if err := dashboardID.UnmarshalText([]byte(opts.DashboardID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("dashboard-id is not a valid UUID: %v", err)),
			opts.JSONOut)
	}
	body, anySet, err := buildEditBody(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if !anySet {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit requires at least one field flag to change"), opts.JSONOut)
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
	resp, err := patchDashboard(cmd.Context(), backplaneURL, dashboardID, tenantFilter, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP 200 without a dashboard payload", backplaneURL)),
			opts.JSONOut)
	}
	entry := resp.JSON200
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "updated dashboard %s (%s)\n", sanitizeCell(entry.Name), entry.Id.String())
	printDashboardSummary(cmd.OutOrStdout(), entry)
	printMemberTable(cmd.OutOrStdout(), entry.Members)
	output.WriteAuditReceipt(cmd.OutOrStdout(), output.AuditIDFromResponse(resp.HTTPResponse))
	return nil
}

// parseSensorIDs turns repeated --add-sensor / --remove-sensor values into
// UUIDs, so a malformed id fails here and not after a round-trip.
func parseSensorIDs(flag string, raw []string) ([]openapi_types.UUID, error) {
	if int64(len(raw)) > maxDashboardMembers {
		return nil, fmt.Errorf("--%s may be given at most %d times; got %d",
			flag, maxDashboardMembers, len(raw))
	}
	ids := make([]openapi_types.UUID, 0, len(raw))
	for _, value := range raw {
		var parsed openapi_types.UUID
		if err := parsed.UnmarshalText([]byte(value)); err != nil {
			return nil, fmt.Errorf("--%s %q is not a valid UUID: %v", flag, value, err)
		}
		ids = append(ids, parsed)
	}
	return ids, nil
}

// buildEditBody puts only the flags the operator set into the PATCH body.
// A nil field is sent as null, which the server reads as "keep the value".
// --notify-off sends an empty notify_email, which turns mail off.
func buildEditBody(opts editOptions) (api.DashboardUpdate, bool, error) {
	body := api.DashboardUpdate{}
	anySet := false
	if opts.isSet("notify-email") && opts.isSet("notify-off") {
		return body, false, fmt.Errorf("pass at most one of --notify-email / --notify-off")
	}
	for _, list := range []struct {
		flag string
		raw  []string
		dst  **[]openapi_types.UUID
	}{
		{"add-sensor", opts.AddSensorIDs, &body.AddSensorIds},
		{"remove-sensor", opts.RemoveSensorIDs, &body.RemoveSensorIds},
	} {
		if !opts.isSet(list.flag) {
			continue
		}
		ids, err := parseSensorIDs(list.flag, list.raw)
		if err != nil {
			return body, false, err
		}
		*list.dst = &ids
		anySet = true
	}
	if opts.isSet("name") {
		name := opts.Name
		body.Name = &name
		anySet = true
	}
	if opts.isSet("notify-email") {
		email := opts.NotifyEmail
		body.NotifyEmail = &email
		anySet = true
	}
	if opts.isSet("notify-off") && opts.NotifyOff {
		off := ""
		body.NotifyEmail = &off
		anySet = true
	}
	if opts.isSet("notify-min-state") {
		if err := validateNotifyMinState(opts.NotifyMinState); err != nil {
			return body, false, err
		}
		minState := api.DashboardUpdateNotifyMinState(opts.NotifyMinState)
		body.NotifyMinState = &minState
		anySet = true
	}
	return body, anySet, nil
}

// patchDashboard calls PATCH /api/v1/checks/dashboards/{id} via the
// generated typed client.
func patchDashboard(
	ctx context.Context,
	backplaneURL string,
	dashboardID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
	body api.DashboardUpdate,
) (*api.EditDashboardApiV1ChecksDashboardsDashboardIdPatchResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.EditDashboardApiV1ChecksDashboardsDashboardIdPatchParams{TenantFilter: tenantFilter}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.EditDashboardApiV1ChecksDashboardsDashboardIdPatchResponse, error) {
			return authed.EditDashboardApiV1ChecksDashboardsDashboardIdPatchWithResponse(
				ctx, dashboardID, params, body)
		},
		func(r *api.EditDashboardApiV1ChecksDashboardsDashboardIdPatchResponse) int {
			return r.StatusCode()
		},
	)
}
