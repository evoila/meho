// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newEditCmd returns the `meho sensor edit` command.
//
//	meho sensor edit <sensor_id> [--name N] [--connector-id ID] [--op-id OP]
//	  [--params @file|<json>] [--assertion @file|<json>]
//	  [--cadence-kind interval|cron] [--interval-seconds N]
//	  [--cron-expr E] [--timezone TZ] [--severity degraded|critical]
//	  [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. Only the flags the operator sets are sent. The id,
// the result history and the dashboard memberships stay.
func newEditCmd() *cobra.Command {
	var opts editOptions
	cmd := &cobra.Command{
		Use:   "edit <sensor_id>",
		Short: "Change some fields of one sensor (tenant_admin)",
		Long: "edit calls PATCH /api/v1/sensors/{id} to change a sensor in " +
			"place. Only the flags you set change. The sensor keeps its id, " +
			"its result history and its place in every dashboard. It runs " +
			"once right after the change. Tenant_admin only.\n\n" +
			"You can change --name, --connector-id, --op-id, --params, " +
			"--assertion, the cadence and --severity. To switch the cadence " +
			"kind, give --cadence-kind with its field: --interval-seconds " +
			"for interval, --cron-expr (and optionally --timezone) for cron.\n\n" +
			"The changed sensor is checked like a new one: the operation must " +
			"exist and be safe (else 422), and the name must be free (else " +
			"409). A cross-tenant / absent id returns 404 sensor_not_found. " +
			"The audit log records the old and new value of each changed " +
			"field.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.SensorID = args[0]
			opts.changed = cmd.Flags().Changed
			return runEdit(cmd, opts)
		},
	}
	f := cmd.Flags()
	f.StringVar(&opts.Name, "name", "", "new sensor name (unique per tenant)")
	f.StringVar(&opts.ConnectorID, "connector-id", "", "new connector id")
	f.StringVar(&opts.OpID, "op-id", "", "new operation id (must be safe)")
	f.StringVar(&opts.ParamsArg, "params", "",
		"new op-params JSON object (inline JSON, @<path>, or @-)")
	f.StringVar(&opts.AssertionArg, "assertion", "",
		"new assertion spec JSON object (inline JSON, @<path>, or @-)")
	f.StringVar(&opts.CadenceKind, "cadence-kind", "", "new cadence kind: interval | cron")
	f.IntVar(&opts.IntervalSeconds, "interval-seconds", 0, "new interval in seconds, 5..86400")
	f.StringVar(&opts.CronExpr, "cron-expr", "", "new 5-field cron expression")
	f.StringVar(&opts.Timezone, "timezone", "", "new IANA timezone for cron evaluation")
	f.StringVar(&opts.Severity, "severity", "", "new severity: degraded | critical")
	f.StringVar(&opts.Tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant edit)")
	f.BoolVar(&opts.JSONOut, "json", false, "emit raw Sensor JSON instead of the human summary")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type editOptions struct {
	SensorID          string
	Name              string
	ConnectorID       string
	OpID              string
	ParamsArg         string
	AssertionArg      string
	CadenceKind       string
	IntervalSeconds   int
	CronExpr          string
	Timezone          string
	Severity          string
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
	var sensorID openapi_types.UUID
	if err := sensorID.UnmarshalText([]byte(opts.SensorID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("sensor-id is not a valid UUID: %v", err)),
			opts.JSONOut)
	}
	body, anySet, err := buildEditBody(cmd, opts)
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
	resp, err := patchSensor(cmd.Context(), backplaneURL, sensorID, tenantFilter, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP 200 without a sensor payload", backplaneURL)),
			opts.JSONOut)
	}
	entry := resp.JSON200
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "updated sensor %s (%s)\n", sanitizeCell(entry.Name), entry.Id.String())
	printSensorSummary(cmd.OutOrStdout(), entry)
	output.WriteAuditReceipt(cmd.OutOrStdout(), output.AuditIDFromResponse(resp.HTTPResponse))
	return nil
}

// buildEditBody puts only the flags the operator set into the PATCH body.
// A nil field is sent as null, which the server reads as "keep the value".
func buildEditBody(cmd *cobra.Command, opts editOptions) (api.SensorUpdate, bool, error) {
	body := api.SensorUpdate{}
	anySet := false
	str := func(flag, value string, dst **string) {
		if opts.isSet(flag) {
			v := value
			*dst = &v
			anySet = true
		}
	}
	str("name", opts.Name, &body.Name)
	str("connector-id", opts.ConnectorID, &body.ConnectorId)
	str("op-id", opts.OpID, &body.OpId)
	str("cron-expr", opts.CronExpr, &body.CronExpr)
	str("timezone", opts.Timezone, &body.Timezone)
	if opts.isSet("params") {
		params, err := loadJSONObjectFlag(cmd, opts.ParamsArg, "--params")
		if err != nil {
			return body, false, err
		}
		if params == nil {
			return body, false, fmt.Errorf("--params must be a JSON object")
		}
		p := map[string]interface{}(params)
		body.Params = &p
		anySet = true
	}
	if opts.isSet("assertion") {
		raw, err := loadJSONObjectBytes(cmd, opts.AssertionArg, "--assertion")
		if err != nil {
			return body, false, err
		}
		if raw == nil {
			return body, false, fmt.Errorf("--assertion must be a JSON object")
		}
		var assertion api.AssertionSpec
		if err := json.Unmarshal(raw, &assertion); err != nil {
			return body, false, fmt.Errorf("--assertion is not a valid assertion spec: %w", err)
		}
		body.Assertion = &assertion
		anySet = true
	}
	if opts.isSet("cadence-kind") {
		if !validCadenceKinds[opts.CadenceKind] {
			return body, false, fmt.Errorf("--cadence-kind must be one of: interval, cron")
		}
		kind := api.SensorCadenceKind(opts.CadenceKind)
		body.CadenceKind = &kind
		anySet = true
	}
	if opts.isSet("interval-seconds") {
		if opts.IntervalSeconds <= 0 {
			return body, false, fmt.Errorf("--interval-seconds must be positive; got %d", opts.IntervalSeconds)
		}
		interval := opts.IntervalSeconds
		body.IntervalSeconds = &interval
		anySet = true
	}
	if opts.isSet("severity") {
		if !validSeverities[opts.Severity] {
			return body, false, fmt.Errorf("--severity must be one of: degraded, critical")
		}
		sev := api.SensorSeverity(opts.Severity)
		body.Severity = &sev
		anySet = true
	}
	return body, anySet, nil
}

// patchSensor calls PATCH /api/v1/sensors/{id} via the generated typed client.
func patchSensor(
	ctx context.Context,
	backplaneURL string,
	sensorID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
	body api.SensorUpdate,
) (*api.EditSensorApiV1SensorsSensorIdPatchResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.EditSensorApiV1SensorsSensorIdPatchParams{TenantFilter: tenantFilter}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.EditSensorApiV1SensorsSensorIdPatchResponse, error) {
			return authed.EditSensorApiV1SensorsSensorIdPatchWithResponse(ctx, sensorID, params, body)
		},
		func(r *api.EditSensorApiV1SensorsSensorIdPatchResponse) int { return r.StatusCode() },
	)
}
