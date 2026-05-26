// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// createRequest mirrors the backend ScheduledTriggerCreate pydantic
// model. The discriminated-union invariant (exactly one of
// cron_expr / fire_at / event_filter populated) is checked CLI-side
// before the request lands; the backend's model validator is the
// ultimate gate.
type createRequest struct {
	Kind              string         `json:"kind"`
	AgentDefinitionID string         `json:"agent_definition_id"`
	CronExpr          *string        `json:"cron_expr,omitempty"`
	FireAt            *string        `json:"fire_at,omitempty"`
	EventFilter       map[string]any `json:"event_filter,omitempty"`
	Timezone          string         `json:"timezone,omitempty"`
	Inputs            map[string]any `json:"inputs,omitempty"`
	IdentitySub       string         `json:"identity_sub,omitempty"`
	InFlightPolicy    string         `json:"in_flight_policy,omitempty"`
	TenantID          *string        `json:"tenant_id,omitempty"`
}

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
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	req := createRequest{
		Kind:              opts.Kind,
		AgentDefinitionID: opts.AgentDefinition,
		Timezone:          opts.Timezone,
		Inputs:            inputs,
		IdentitySub:       opts.IdentitySub,
		InFlightPolicy:    opts.InFlightPolicy,
	}
	if opts.CronExpr != "" {
		cronCopy := opts.CronExpr
		req.CronExpr = &cronCopy
	}
	if opts.FireAt != "" {
		fireAtCopy := opts.FireAt
		req.FireAt = &fireAtCopy
	}
	if eventFilter != nil {
		req.EventFilter = eventFilter
	}
	if opts.Tenant != "" {
		tenantCopy := opts.Tenant
		req.TenantID = &tenantCopy
	}

	entry, err := postCreate(cmd.Context(), backplaneURL, req)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "created %s trigger %s\n", entry.Kind, entry.ID)
	printTriggerSummary(cmd.OutOrStdout(), entry)
	return nil
}

func postCreate(ctx context.Context, backplaneURL string, req createRequest) (*Trigger, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal scheduler create request: %w", err)
	}
	raw, err := doAuthedRequest(ctx, backplaneURL, "POST",
		"/api/v1/scheduler/triggers", body)
	if err != nil {
		return nil, err
	}
	var out Trigger
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode scheduler create response: %w", err)
	}
	return &out, nil
}
