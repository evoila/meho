// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCreateCmd returns the `meho sensor create` command.
//
//	meho sensor create --name N --connector-id ID --op-id OP
//	  --assertion @file|<json> --cadence-kind interval|cron
//	  (--interval-seconds N | --cron-expr E [--timezone TZ])
//	  [--severity degraded|critical] [--for-seconds N]
//	  [--target @file|<json>] [--params @file|<json>] [--identity-sub SUB]
//	  [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin.
func newCreateCmd() *cobra.Command {
	var (
		name              string
		connectorID       string
		opID              string
		assertionArg      string
		cadenceKind       string
		intervalSeconds   int
		cronExpr          string
		timezone          string
		severity          string
		forSeconds        int
		targetArg         string
		paramsArg         string
		identitySub       string
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create one sensor (tenant_admin)",
		Long: "create calls POST /api/v1/sensors to create one sensor. " +
			"Tenant_admin only — operator-role JWT lands as 403 " +
			"insufficient_role.\n\n" +
			"--name is the operator-facing handle (unique per tenant). " +
			"--connector-id + --op-id name the operation; it MUST resolve to " +
			"a safety_level='safe' descriptor (a caution/dangerous or unknown " +
			"op is refused 422). --assertion is a bounded select->compare " +
			"spec (inline JSON, @<path>, or @-). --cadence-kind is " +
			"interval|cron; provide exactly one of:\n" +
			"  --interval-seconds N   (kind=interval, 5..86400)\n" +
			"  --cron-expr <expr>     (kind=cron, 5-field cron)\n" +
			"--timezone is the IANA zone for cron evaluation (default 'UTC'). " +
			"--severity is the worst rollup state a failure drives " +
			"(degraded|critical, default critical). --for-seconds is the " +
			"hold-time hysteresis (default 0). --target / --params are " +
			"optional JSON objects. --identity-sub overrides the default " +
			"runner identity. --tenant targets another tenant (platform_admin " +
			"cross-tenant).\n\nA non-safe op returns 422 " +
			"sensor_requires_safe_operation; an unknown op 422 " +
			"sensor_operation_not_found; a duplicate name 409 " +
			"sensor_name_conflict.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runCreate(cmd, createOptions{
				Name:              name,
				ConnectorID:       connectorID,
				OpID:              opID,
				AssertionArg:      assertionArg,
				CadenceKind:       cadenceKind,
				IntervalSeconds:   intervalSeconds,
				CronExpr:          cronExpr,
				Timezone:          timezone,
				Severity:          severity,
				ForSeconds:        forSeconds,
				TargetArg:         targetArg,
				ParamsArg:         paramsArg,
				IdentitySub:       identitySub,
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "operator-facing sensor name (unique per tenant)")
	cmd.Flags().StringVar(&connectorID, "connector-id", "", "connector id of the operation to evaluate")
	cmd.Flags().StringVar(&opID, "op-id", "", "operation id (must be safety_level='safe')")
	cmd.Flags().StringVar(&assertionArg, "assertion", "",
		"bounded select->compare assertion spec JSON object (inline JSON, @<path>, or @-)")
	cmd.Flags().StringVar(&cadenceKind, "cadence-kind", "", "cadence kind: interval | cron")
	cmd.Flags().IntVar(&intervalSeconds, "interval-seconds", 0,
		"interval in seconds, 5..86400 (required when --cadence-kind=interval)")
	cmd.Flags().StringVar(&cronExpr, "cron-expr", "",
		"5-field cron expression (required when --cadence-kind=cron)")
	cmd.Flags().StringVar(&timezone, "timezone", "",
		"IANA timezone name for cron evaluation (default 'UTC')")
	cmd.Flags().StringVar(&severity, "severity", "",
		"worst rollup state a failing assertion drives: degraded | critical (default critical)")
	cmd.Flags().IntVar(&forSeconds, "for-seconds", 0,
		"hold-time hysteresis in seconds a failing state must persist (default 0)")
	cmd.Flags().StringVar(&targetArg, "target", "",
		"optional dispatch-target JSON object (inline JSON, @<path>, or @-)")
	cmd.Flags().StringVar(&paramsArg, "params", "",
		"optional op-params JSON object (inline JSON, @<path>, or @-)")
	cmd.Flags().StringVar(&identitySub, "identity-sub", "",
		"identity sub the runner dispatches under (default '__sensor__')")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant create)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw Sensor JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("name")
	_ = cmd.MarkFlagRequired("connector-id")
	_ = cmd.MarkFlagRequired("op-id")
	_ = cmd.MarkFlagRequired("assertion")
	_ = cmd.MarkFlagRequired("cadence-kind")
	return cmd
}

type createOptions struct {
	Name              string
	ConnectorID       string
	OpID              string
	AssertionArg      string
	CadenceKind       string
	IntervalSeconds   int
	CronExpr          string
	Timezone          string
	Severity          string
	ForSeconds        int
	TargetArg         string
	ParamsArg         string
	IdentitySub       string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

func runCreate(cmd *cobra.Command, opts createOptions) error {
	if !validCadenceKinds[opts.CadenceKind] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--cadence-kind must be one of: interval, cron"), opts.JSONOut)
	}
	if opts.Severity != "" && !validSeverities[opts.Severity] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--severity must be one of: degraded, critical"), opts.JSONOut)
	}
	// Per-cadence discriminator pre-check (the backend's Pydantic validator
	// is the ultimate gate; checking here gives immediate rejection).
	switch opts.CadenceKind {
	case "interval":
		if opts.IntervalSeconds <= 0 {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--cadence-kind=interval requires --interval-seconds"), opts.JSONOut)
		}
		if strings.TrimSpace(opts.CronExpr) != "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--cadence-kind=interval forbids --cron-expr"), opts.JSONOut)
		}
	case "cron":
		if strings.TrimSpace(opts.CronExpr) == "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--cadence-kind=cron requires --cron-expr"), opts.JSONOut)
		}
		if opts.IntervalSeconds > 0 {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--cadence-kind=cron forbids --interval-seconds"), opts.JSONOut)
		}
	}
	// Parse the assertion spec CLI-side into the generated union type so a
	// malformed spec surfaces locally rather than after a 422 round-trip.
	assertionBytes, err := loadJSONObjectBytes(cmd, opts.AssertionArg, "--assertion")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if assertionBytes == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--assertion is required"), opts.JSONOut)
	}
	var assertion api.AssertionSpec
	if err := json.Unmarshal(assertionBytes, &assertion); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--assertion is not a valid assertion spec: %v", err)),
			opts.JSONOut)
	}
	target, err := loadJSONObjectFlag(cmd, opts.TargetArg, "--target")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	params, err := loadJSONObjectFlag(cmd, opts.ParamsArg, "--params")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	var tenantID *openapi_types.UUID
	if opts.Tenant != "" {
		var parsed openapi_types.UUID
		if err := parsed.UnmarshalText([]byte(opts.Tenant)); err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("--tenant is not a valid UUID: %v", err)),
				opts.JSONOut)
		}
		tenantID = &parsed
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	body := buildCreateBody(opts, assertion, tenantID, target, params)
	resp, err := postCreate(cmd.Context(), backplaneURL, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 201 + missing-content-type leaving JSON201 nil.
	if resp.JSON201 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without a created-sensor payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	entry := resp.JSON201
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "created sensor %s (%s)\n", entry.Name, entry.Id.String())
	printSensorSummary(cmd.OutOrStdout(), entry)
	return nil
}

// buildCreateBody assembles the typed POST body. Pulled out so the
// wire-shape rendering (cadence discriminator, pointer-or-omit semantics)
// stays unit-testable without an httptest.Server.
func buildCreateBody(
	opts createOptions,
	assertion api.AssertionSpec,
	tenantID *openapi_types.UUID,
	target map[string]any,
	params map[string]any,
) api.SensorCreate {
	body := api.SensorCreate{
		Name:        opts.Name,
		ConnectorId: opts.ConnectorID,
		OpId:        opts.OpID,
		Assertion:   assertion,
		CadenceKind: api.SensorCadenceKind(opts.CadenceKind),
	}
	if opts.IntervalSeconds > 0 {
		interval := opts.IntervalSeconds
		body.IntervalSeconds = &interval
	}
	if opts.CronExpr != "" {
		cronCopy := opts.CronExpr
		body.CronExpr = &cronCopy
	}
	if opts.Timezone != "" {
		tz := opts.Timezone
		body.Timezone = &tz
	}
	if opts.Severity != "" {
		sev := api.SensorSeverity(opts.Severity)
		body.Severity = &sev
	}
	if opts.ForSeconds > 0 {
		fs := opts.ForSeconds
		body.ForSeconds = &fs
	}
	if target != nil {
		t := map[string]interface{}(target)
		body.Target = &t
	}
	if params != nil {
		p := map[string]interface{}(params)
		body.Params = &p
	}
	if opts.IdentitySub != "" {
		sub := opts.IdentitySub
		body.IdentitySub = &sub
	}
	if tenantID != nil {
		body.TenantId = tenantID
	}
	return body
}

// postCreate calls POST /api/v1/sensors via the generated typed client.
func postCreate(
	ctx context.Context,
	backplaneURL string,
	body api.SensorCreate,
) (*api.CreateSensorApiV1SensorsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.CreateSensorApiV1SensorsPostResponse, error) {
			return authed.CreateSensorApiV1SensorsPostWithResponse(ctx, nil, body)
		},
		func(r *api.CreateSensorApiV1SensorsPostResponse) int { return r.StatusCode() },
	)
}
