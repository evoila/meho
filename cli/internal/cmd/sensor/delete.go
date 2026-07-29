// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"context"
	"fmt"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newDeleteCmd returns the `meho sensor delete` command.
//
//	meho sensor delete <sensor_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. Hard-deletes the sensor row (no tombstone).
// A cross-tenant / absent id returns 404 sensor_not_found.
func newDeleteCmd() *cobra.Command {
	var (
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <sensor_id>",
		Short: "Delete one sensor by id (tenant_admin)",
		Long: "delete calls DELETE /api/v1/sensors/{id} to hard-delete a " +
			"sensor (no tombstone row is retained). Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"A cross-tenant / absent id returns 404 sensor_not_found " +
			"(existence is not leaked across tenants).\n\n" +
			"--tenant targets another tenant (platform_admin cross-tenant " +
			"delete).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDelete(cmd, deleteOptions{
				SensorID:          args[0],
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
	SensorID          string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runDelete(cmd *cobra.Command, opts deleteOptions) error {
	if opts.SensorID == "" {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("delete requires a non-empty <sensor_id> argument"), opts.JSONOut)
	}
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
	resp, err := deleteSensor(cmd.Context(), backplaneURL, sensorID, tenantFilter)
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
			map[string]any{"sensor_id": opts.SensorID, "deleted": true})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted sensor %s\n", opts.SensorID)
	return nil
}

// deleteSensor calls DELETE /api/v1/sensors/{id} via the generated typed
// client.
func deleteSensor(
	ctx context.Context,
	backplaneURL string,
	sensorID openapi_types.UUID,
	tenantFilter *openapi_types.UUID,
) (*api.DeleteSensorApiV1SensorsSensorIdDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := &api.DeleteSensorApiV1SensorsSensorIdDeleteParams{}
	if tenantFilter != nil {
		params.TenantFilter = tenantFilter
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.DeleteSensorApiV1SensorsSensorIdDeleteResponse, error) {
			return authed.DeleteSensorApiV1SensorsSensorIdDeleteWithResponse(ctx, sensorID, params)
		},
		func(r *api.DeleteSensorApiV1SensorsSensorIdDeleteResponse) int { return r.StatusCode() },
	)
}
