// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCreateCmd returns the `meho scheduler create` command.
//
//	meho scheduler create --kind K --agent-definition ID
//	  [--cron-expr E | --fire-at TS | --event-filter @file|<json>]
//	  [--timezone TZ] [--inputs @file|<json>] [--identity-sub SUB]
//	  [--in-flight-policy P] [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin.
func newCreateCmd() *cobra.Command {
	var (
		kind              string
		agentDefinition   string
		cronExpr          string
		fireAt            string
		eventFilter       string
		timezone          string
		inputsArg         string
		identitySub       string
		inFlightPolicy    string
		tenant            string
		workRef           string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create one scheduled trigger (tenant_admin)",
		Long: "create calls POST /api/v1/scheduler/triggers to create " +
			"one scheduled trigger. Tenant_admin only — operator-role " +
			"JWT lands as 403 insufficient_role.\n\n" +
			"--kind is one of cron|one_off|event. --agent-definition is " +
			"the UUID of an existing agent definition in the target " +
			"tenant. Exactly one of:\n" +
			"  --cron-expr <expr>     (kind=cron, 5-field cron)\n" +
			"  --fire-at <ISO8601>    (kind=one_off)\n" +
			"  --event-filter <json>  (kind=event; inline JSON, @<path>, or @-)\n" +
			"--timezone is the IANA timezone for cron evaluation " +
			"(default 'UTC'). --inputs is an optional JSON object " +
			"forwarded as the agent run's input. --identity-sub overrides " +
			"the default scheduler identity. --in-flight-policy is one of " +
			"fail_into_audit|resume (default fail_into_audit). --tenant " +
			"targets another tenant (tenant_admin can act cross-tenant). " +
			"--work-ref pins an external change-ticket reference (e.g. " +
			"gh:evoila/meho#13) on the trigger; every run it dispatches " +
			"inherits it on the run row and its audit trail. " +
			"\n\nAn unknown --agent-definition returns 422 " +
			"agent_definition_not_found; an invalid cron expression " +
			"or timezone returns 422 invalid_arguments.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runCreate(cmd, createOptions{
				Kind:              kind,
				AgentDefinition:   agentDefinition,
				CronExpr:          cronExpr,
				FireAt:            fireAt,
				EventFilterArg:    eventFilter,
				Timezone:          timezone,
				InputsArg:         inputsArg,
				IdentitySub:       identitySub,
				InFlightPolicy:    inFlightPolicy,
				Tenant:            tenant,
				WorkRef:           workRef,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&kind, "kind", "",
		"trigger kind: cron | one_off | event")
	cmd.Flags().StringVar(&agentDefinition, "agent-definition", "",
		"UUID of the agent definition to fire")
	cmd.Flags().StringVar(&cronExpr, "cron-expr", "",
		"5-field cron expression (required when --kind=cron)")
	cmd.Flags().StringVar(&fireAt, "fire-at", "",
		"ISO 8601 fire time (required when --kind=one_off)")
	cmd.Flags().StringVar(&eventFilter, "event-filter", "",
		"event-match filter JSON object (required when --kind=event; inline JSON, @<path>, or @-)")
	cmd.Flags().StringVar(&timezone, "timezone", "",
		"IANA timezone name for cron evaluation (default 'UTC')")
	cmd.Flags().StringVar(&inputsArg, "inputs", "",
		"optional inputs JSON object forwarded as the agent run's input (inline JSON, @<path>, or @-)")
	cmd.Flags().StringVar(&identitySub, "identity-sub", "",
		"identity sub the scheduler impersonates at fire time (default '__scheduler__')")
	cmd.Flags().StringVar(&inFlightPolicy, "in-flight-policy", "",
		"killed-mid-flight policy: fail_into_audit | resume (default fail_into_audit)")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (tenant_admin cross-tenant create)")
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"external change-ticket reference inherited by every dispatched run (e.g. gh:evoila/meho#13)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw Trigger JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("kind")
	_ = cmd.MarkFlagRequired("agent-definition")
	return cmd
}

type createOptions struct {
	Kind              string
	AgentDefinition   string
	CronExpr          string
	FireAt            string
	EventFilterArg    string
	Timezone          string
	InputsArg         string
	IdentitySub       string
	InFlightPolicy    string
	Tenant            string
	WorkRef           string
	JSONOut           bool
	BackplaneOverride string
}

func runCreate(cmd *cobra.Command, opts createOptions) error {
	if !validKinds[opts.Kind] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--kind must be one of: cron, one_off, event"), opts.JSONOut)
	}
	if opts.InFlightPolicy != "" && !validInFlightPolicies[opts.InFlightPolicy] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--in-flight-policy must be one of: fail_into_audit, resume"),
			opts.JSONOut)
	}
	// Per-kind discriminator pre-check (the backend's Pydantic
	// validator is the ultimate gate; checking here gives the
	// operator immediate rejection instead of a remote 422).
	switch opts.Kind {
	case "cron":
		if strings.TrimSpace(opts.CronExpr) == "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--kind=cron requires --cron-expr"), opts.JSONOut)
		}
		if opts.FireAt != "" || opts.EventFilterArg != "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--kind=cron forbids --fire-at and --event-filter"),
				opts.JSONOut)
		}
	case "one_off":
		if strings.TrimSpace(opts.FireAt) == "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--kind=one_off requires --fire-at"), opts.JSONOut)
		}
		if opts.CronExpr != "" || opts.EventFilterArg != "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--kind=one_off forbids --cron-expr and --event-filter"),
				opts.JSONOut)
		}
	case "event":
		if strings.TrimSpace(opts.EventFilterArg) == "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--kind=event requires --event-filter"), opts.JSONOut)
		}
		if opts.CronExpr != "" || opts.FireAt != "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected("--kind=event forbids --cron-expr and --fire-at"),
				opts.JSONOut)
		}
	}
	eventFilter, err := loadJSONObjectFlag(cmd, opts.EventFilterArg, "--event-filter")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	inputs, err := loadJSONObjectFlag(cmd, opts.InputsArg, "--inputs")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	// Parse --agent-definition and --tenant CLI-side so malformed
	// UUIDs surface locally as a clear error rather than after the
	// server's 422 round-trip. The generated `ScheduledTriggerCreate`
	// requires `openapi_types.UUID` for both fields.
	var agentDefID openapi_types.UUID
	if err := agentDefID.UnmarshalText([]byte(opts.AgentDefinition)); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--agent-definition is not a valid UUID: %v", err)),
			opts.JSONOut)
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
	// Parse --fire-at CLI-side. The generated `ScheduledTriggerCreate.FireAt`
	// is `*time.Time`; the typed client serialises that as the wire
	// shape the backend's pydantic `datetime` validator expects (ISO
	// 8601). Accept either RFC3339 (with timezone) or the
	// timezone-less form the consumer doc mentions; the backend
	// rejects naive datetimes with 422 invalid_arguments.
	var fireAtTime *time.Time
	if opts.FireAt != "" {
		parsed, perr := time.Parse(time.RFC3339Nano, opts.FireAt)
		if perr != nil {
			// Fall back to the simpler RFC3339 (no nanoseconds) form.
			parsed2, perr2 := time.Parse(time.RFC3339, opts.FireAt)
			if perr2 != nil {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf(
						"--fire-at must be RFC 3339 (e.g. 2026-01-15T12:00:00Z): %v", perr)),
					opts.JSONOut)
			}
			parsed = parsed2
		}
		fireAtTime = &parsed
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	body := buildCreateBody(opts, agentDefID, tenantID, fireAtTime, eventFilter, inputs)
	resp, err := postCreate(cmd.Context(), backplaneURL, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 201 + missing-content-type leaving JSON201 nil
	// (oapi-codegen only populates the typed field when the
	// response advertises `application/json`). Without this guard
	// a malformed 201 would pass `entry` as nil into
	// `printTriggerSummary`, which short-circuits — operator sees
	// the "created" prose with no follow-up summary as if every
	// field were empty.
	if resp.JSON201 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without a created-trigger payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	entry := resp.JSON201
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "created %s trigger %s\n", string(entry.Kind), entry.Id.String())
	printTriggerSummary(cmd.OutOrStdout(), entry)
	return nil
}

// buildCreateBody assembles the typed POST body. Pulled out so the
// wire-shape rendering (kind enum, pointer-or-omit semantics on
// nullable fields, conditional `tenant_id` for cross-tenant admin)
// stays unit-testable without spinning up an httptest.Server.
//
// Wire-shape note: the generated `api.ScheduledTriggerCreate` types
// `CronExpr`, `FireAt`, `EventFilter`, `Inputs`, `TenantId` as
// nullable pointers (no `omitempty` on their JSON tags); the
// pre-migration `createRequest` carried `,omitempty` and dropped the
// keys for the nil case. The backend's pydantic validator treats
// `null` and absent identically for these fields (its model marks
// them `Optional[X] = None`), so the wire-level switch from "drop"
// to "explicit null" is behaviour-preserving on the server. The
// pre-condition pre-check in `runCreate` still rejects forbidden-
// for-this-kind fields locally so the wire never sends a
// `cron_expr=null` + `fire_at=<value>` combination the backend's
// discriminated-union validator would reject as 422.
func buildCreateBody(
	opts createOptions,
	agentDefID openapi_types.UUID,
	tenantID *openapi_types.UUID,
	fireAtTime *time.Time,
	eventFilter map[string]any,
	inputs map[string]any,
) api.ScheduledTriggerCreate {
	body := api.ScheduledTriggerCreate{
		AgentDefinitionId: agentDefID,
		Kind:              api.ScheduledTriggerKind(opts.Kind),
	}
	if opts.CronExpr != "" {
		cronCopy := opts.CronExpr
		body.CronExpr = &cronCopy
	}
	if fireAtTime != nil {
		body.FireAt = fireAtTime
	}
	if eventFilter != nil {
		ef := map[string]interface{}(eventFilter)
		body.EventFilter = &ef
	}
	if opts.Timezone != "" {
		tz := opts.Timezone
		body.Timezone = &tz
	}
	if inputs != nil {
		in := map[string]interface{}(inputs)
		body.Inputs = &in
	}
	if opts.IdentitySub != "" {
		sub := opts.IdentitySub
		body.IdentitySub = &sub
	}
	if opts.InFlightPolicy != "" {
		policy := api.ScheduledTriggerInFlightPolicy(opts.InFlightPolicy)
		body.InFlightPolicy = &policy
	}
	if tenantID != nil {
		body.TenantId = tenantID
	}
	if opts.WorkRef != "" {
		wr := opts.WorkRef
		body.WorkRef = &wr
	}
	return body
}

// postCreate calls POST /api/v1/scheduler/triggers via the generated
// typed client. The 401-refresh-retry loop runs through retryOn401.
func postCreate(
	ctx context.Context,
	backplaneURL string,
	body api.ScheduledTriggerCreate,
) (*api.CreateTriggerApiV1SchedulerTriggersPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.CreateTriggerApiV1SchedulerTriggersPostResponse, error) {
			return authed.CreateTriggerApiV1SchedulerTriggersPostWithResponse(ctx, nil, body)
		},
		func(r *api.CreateTriggerApiV1SchedulerTriggersPostResponse) int { return r.StatusCode() },
	)
}
