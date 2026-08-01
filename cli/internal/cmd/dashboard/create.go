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

// newCreateCmd returns the `meho dashboard create` command.
//
//	meho dashboard create --name N [--description D]
//	  [--sensor-id ID ...] [--notify-email A] [--notify-min-state S]
//	  [--investigator-prompt P] [--tenant T] [--json] [--backplane <url>]
//
// Role: tenant_admin.
func newCreateCmd() *cobra.Command {
	var (
		name               string
		description        string
		sensorIDs          []string
		notifyEmail        string
		notifyMinState     string
		investigatorPrompt string
		tenant             string
		jsonOut            bool
		backplaneOverride  string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create one dashboard (tenant_admin)",
		Long: "create calls POST /api/v1/checks/dashboards to compose one " +
			"dashboard from registered Sensors. Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"--name is the operator-facing handle (unique per tenant). " +
			"--description is optional free-form prose. --sensor-id names one " +
			"member Sensor and is repeatable; membership is set at create " +
			"only (there is no edit verb — \"edit\" is delete + recreate). An " +
			"empty member set is legal and rolls up 'unknown' (the zero-member " +
			"rule); duplicate ids are de-duplicated server-side. --tenant " +
			"targets another tenant (platform_admin cross-tenant create).\n\n" +
			"--notify-email is the single address that receives one mail per " +
			"rollup transition crossing --notify-min-state; leaving it unset " +
			"keeps notifications off. --notify-min-state is degraded or " +
			"critical (default critical): an edge notifies when the worse of " +
			"its two states reaches the floor, so at 'critical' a recovery " +
			"from critical back to ok sends the all-clear while ok -> " +
			"degraded stays silent. Both are set at create only, like " +
			"membership.\n\n" +
			"--investigator-prompt adds operator context to the briefing the " +
			"diagnose-only investigator receives when this dashboard goes " +
			"non-green: what this dashboard means, where to look first, what is " +
			"known to be noisy. It is appended after the server-built transition " +
			"snapshot in a clearly delimited section, so the deterministic facts " +
			"always lead and the prompt cannot replace them. Capped at 4096 " +
			"characters; a longer one is refused 422.\n\n" +
			"A foreign / absent --sensor-id is refused 422 sensor_not_found; a " +
			"duplicate name is refused 409.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runCreate(cmd, createOptions{
				Name:               name,
				Description:        description,
				SensorIDs:          sensorIDs,
				NotifyEmail:        notifyEmail,
				NotifyMinState:     notifyMinState,
				InvestigatorPrompt: investigatorPrompt,
				Tenant:             tenant,
				JSONOut:            jsonOut,
				BackplaneOverride:  backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&name, "name", "", "operator-facing dashboard name (unique per tenant)")
	cmd.Flags().StringVar(&description, "description", "", "optional free-form description")
	cmd.Flags().StringArrayVar(&sensorIDs, "sensor-id", nil,
		"member sensor UUID (repeatable; empty set rolls up 'unknown')")
	cmd.Flags().StringVar(&notifyEmail, "notify-email", "",
		"address that receives transition mail (unset = notifications off)")
	cmd.Flags().StringVar(&notifyMinState, "notify-min-state", "",
		"notification floor: degraded or critical (server default: critical)")
	cmd.Flags().StringVar(&investigatorPrompt, "investigator-prompt", "",
		"operator context appended to the investigator's briefing (max 4096 chars)")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (platform_admin cross-tenant create)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit raw DashboardDetail JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("name")
	return cmd
}

type createOptions struct {
	Name               string
	Description        string
	SensorIDs          []string
	NotifyEmail        string
	NotifyMinState     string
	InvestigatorPrompt string
	Tenant             string
	JSONOut            bool
	BackplaneOverride  string
}

// notifyMinStates is the closed --notify-min-state vocabulary, mirroring the
// server's ck_check_dashboards_notify_min_state CHECK. Validated CLI-side so
// a typo surfaces locally instead of after a 422 round-trip — the same
// discipline the --sensor-id UUID parse follows.
var notifyMinStates = []api.DashboardCreateNotifyMinState{
	api.DashboardCreateNotifyMinStateDegraded,
	api.DashboardCreateNotifyMinStateCritical,
}

func runCreate(cmd *cobra.Command, opts createOptions) error {
	if int64(len(opts.SensorIDs)) > maxDashboardMembers {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--sensor-id may be given at most %d times; got %d",
				maxDashboardMembers, len(opts.SensorIDs))),
			opts.JSONOut)
	}
	if err := validateNotifyMinState(opts.NotifyMinState); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	// Parse each --sensor-id CLI-side into a typed UUID so a malformed id
	// surfaces locally rather than after a 422 round-trip.
	sensorIDs := make([]openapi_types.UUID, 0, len(opts.SensorIDs))
	for _, raw := range opts.SensorIDs {
		var parsed openapi_types.UUID
		if err := parsed.UnmarshalText([]byte(raw)); err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf("--sensor-id %q is not a valid UUID: %v", raw, err)),
				opts.JSONOut)
		}
		sensorIDs = append(sensorIDs, parsed)
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

	body := buildCreateBody(opts, sensorIDs, tenantID)
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
				"call %s: HTTP 201 without a created-dashboard payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	entry := resp.JSON201
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), entry)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "created dashboard %s (%s)\n", entry.Name, entry.Id.String())
	printDashboardSummary(cmd.OutOrStdout(), entry)
	printMemberTable(cmd.OutOrStdout(), entry.Members)
	return nil
}

// validateNotifyMinState rejects a --notify-min-state outside the closed
// server vocabulary. An empty value is legal and means "omit the field", so
// the server's own default (critical) applies.
func validateNotifyMinState(value string) error {
	if value == "" {
		return nil
	}
	for _, allowed := range notifyMinStates {
		if value == string(allowed) {
			return nil
		}
	}
	return fmt.Errorf("--notify-min-state must be one of degraded, critical; got %q", value)
}

// buildCreateBody assembles the typed POST body. Pulled out so the
// wire-shape rendering (pointer-or-omit semantics for the optional fields)
// stays unit-testable without an httptest.Server. An empty member slice is
// forwarded as an empty (non-nil) sensor_ids so the zero-member rule applies
// deterministically rather than depending on the field being omitted.
//
// notify_min_state is genuinely omitted when the flag is unset -- it is the
// one optional field whose generated tag carries omitempty -- so the server's
// default stays the single source of that value and an explicit null can
// never 422 its defaulted Literal. notify_email and investigator_prompt have
// no omitempty, so unset means nil means an explicit JSON null on the wire;
// both are `str | None` server-side, where null and absent read alike, so the
// row keeps notifications off and keeps the pre-#2721 briefing respectively.
// An unset --notify-email leaves the field nil rather than sending an empty
// openapi_types.Email, whose MarshalJSON would reject the empty string before
// the request goes out.
//
// investigator_prompt's 4096-character cap is deliberately NOT re-checked
// here: unlike --notify-min-state's closed vocabulary (a typo the CLI can
// name precisely), a length bound duplicated client-side drifts silently
// the moment the server's cap changes, and the server's 422 already names
// the field and the limit.
func buildCreateBody(
	opts createOptions,
	sensorIDs []openapi_types.UUID,
	tenantID *openapi_types.UUID,
) api.DashboardCreate {
	ids := sensorIDs
	body := api.DashboardCreate{
		Name:      opts.Name,
		SensorIds: &ids,
	}
	if opts.Description != "" {
		desc := opts.Description
		body.Description = &desc
	}
	if tenantID != nil {
		body.TenantId = tenantID
	}
	if opts.NotifyEmail != "" {
		email := openapi_types.Email(opts.NotifyEmail)
		body.NotifyEmail = &email
	}
	if opts.NotifyMinState != "" {
		minState := api.DashboardCreateNotifyMinState(opts.NotifyMinState)
		body.NotifyMinState = &minState
	}
	if opts.InvestigatorPrompt != "" {
		prompt := opts.InvestigatorPrompt
		body.InvestigatorPrompt = &prompt
	}
	return body
}

// postCreate calls POST /api/v1/checks/dashboards via the generated typed
// client.
func postCreate(
	ctx context.Context,
	backplaneURL string,
	body api.DashboardCreate,
) (*api.CreateDashboardApiV1ChecksDashboardsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.CreateDashboardApiV1ChecksDashboardsPostResponse, error) {
			return authed.CreateDashboardApiV1ChecksDashboardsPostWithResponse(ctx, nil, body)
		},
		func(r *api.CreateDashboardApiV1ChecksDashboardsPostResponse) int { return r.StatusCode() },
	)
}
