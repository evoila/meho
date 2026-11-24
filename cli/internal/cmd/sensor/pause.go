// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"fmt"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newPauseCmd returns the `meho sensor pause` command.
//
//	meho sensor pause <sensor_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. A paused sensor is not run, so it does not call its
// target. Its id, result history and dashboard places stay.
func newPauseCmd() *cobra.Command {
	return newStatusCmd(
		"pause",
		api.SensorUpdateStatusPaused,
		"Pause one sensor (tenant_admin)",
		"pause calls PATCH /api/v1/sensors/{id} with status=paused. A paused "+
			"sensor is not run, so it does not call its target. It keeps its "+
			"id, its result history and its place in every dashboard. Use it "+
			"while you fix the credential of its target. Safe to run twice. "+
			"Tenant_admin only.",
		"paused sensor",
	)
}

// newResumeCmd returns the `meho sensor resume` command.
//
//	meho sensor resume <sensor_id> [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin. The sensor runs again at once.
func newResumeCmd() *cobra.Command {
	return newStatusCmd(
		"resume",
		api.SensorUpdateStatusActive,
		"Resume a paused sensor (tenant_admin)",
		"resume calls PATCH /api/v1/sensors/{id} with status=active. The "+
			"sensor runs again at once. If the credential of its target is "+
			"still wrong, MEHO pauses it again after a few refused logins. "+
			"Safe to run twice. Tenant_admin only.",
		"resumed sensor",
	)
}

type statusOptions struct {
	SensorID          string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func newStatusCmd(
	verb string,
	status api.SensorUpdateStatus,
	short, long, done string,
) *cobra.Command {
	var opts statusOptions
	cmd := &cobra.Command{
		Use:           verb + " <sensor_id>",
		Short:         short,
		Long:          long,
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.SensorID = args[0]
			return runStatus(cmd, opts, status, done)
		},
	}
	f := cmd.Flags()
	f.StringVar(&opts.Tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant change)")
	f.BoolVar(&opts.JSONOut, "json", false, "emit raw Sensor JSON instead of the human summary")
	f.StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runStatus(cmd *cobra.Command, opts statusOptions, status api.SensorUpdateStatus, done string) error {
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
	resp, err := patchSensor(cmd.Context(), backplaneURL, sensorID, tenantFilter,
		api.SensorUpdate{Status: &status})
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
	fmt.Fprintf(cmd.OutOrStdout(), "%s %s (%s)\n", done, sanitizeCell(entry.Name), entry.Id.String())
	printSensorSummary(cmd.OutOrStdout(), entry)
	output.WriteAuditReceipt(cmd.OutOrStdout(), output.AuditIDFromResponse(resp.HTTPResponse))
	return nil
}
