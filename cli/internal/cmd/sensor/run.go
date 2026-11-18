// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

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

// newRunNowCmd returns the `meho sensor run` command.
//
//	meho sensor run <sensor_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. The command makes the sensor due at once. The runner
// runs it at its next tick (10 seconds by default).
func newRunNowCmd() *cobra.Command {
	var opts runNowOptions
	cmd := &cobra.Command{
		Use:   "run <sensor_id>",
		Short: "Run one sensor once now (tenant_admin)",
		Long: "run calls POST /api/v1/sensors/{id}/run. MEHO makes the sensor " +
			"due at once, and the runner runs it at its next tick (10 seconds " +
			"by default), with its normal login and rules. The command does " +
			"not wait for the result: read it with `meho sensor results <id>`. " +
			"A paused sensor is refused: resume it first. Safe to run twice. " +
			"Tenant_admin only.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.SensorID = args[0]
			return runSensorNow(cmd, opts)
		},
	}
	f := cmd.Flags()
	f.StringVar(&opts.Tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant run)")
	f.BoolVar(&opts.JSONOut, "json", false, "emit a structured JSON result instead of plain text")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type runNowOptions struct {
	SensorID          string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runSensorNow(cmd *cobra.Command, opts runNowOptions) error {
	var sensorID openapi_types.UUID
	if err := sensorID.UnmarshalText([]byte(opts.SensorID)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("sensor-id is not a valid UUID: %v", err)),
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
	resp, err := postSensorRun(cmd.Context(), backplaneURL, sensorID, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusAccepted {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON202 == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP 202 without a sensor payload", backplaneURL)),
			opts.JSONOut)
	}
	accepted := resp.JSON202
	auditID := output.AuditIDFromResponse(resp.HTTPResponse)
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), map[string]any{
			"sensor_id":    accepted.SensorId.String(),
			"next_fire_at": accepted.NextFireAt,
			"audit_id":     auditID,
		})
	}
	fmt.Fprintf(cmd.OutOrStdout(),
		"sensor %s runs at the next runner tick; read the result with `meho sensor results %s`\n",
		accepted.SensorId.String(), accepted.SensorId.String())
	output.WriteAuditReceipt(cmd.OutOrStdout(), auditID)
	return nil
}

// postSensorRun calls POST /api/v1/sensors/{id}/run via the generated typed
// client.
func postSensorRun(
	ctx context.Context,
	backplaneURL string,
	sensorID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
) (*api.RunSensorNowApiV1SensorsSensorIdRunPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.RunSensorNowApiV1SensorsSensorIdRunPostParams{TenantFilter: tenantFilter}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.RunSensorNowApiV1SensorsSensorIdRunPostResponse, error) {
			return authed.RunSensorNowApiV1SensorsSensorIdRunPostWithResponse(ctx, sensorID, params)
		},
		func(r *api.RunSensorNowApiV1SensorsSensorIdRunPostResponse) int { return r.StatusCode() },
	)
}
