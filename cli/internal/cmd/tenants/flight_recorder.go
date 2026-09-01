// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package tenants

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newFlightRecorderPolicyCmd returns `meho tenants flight-recorder-policy`.
func newFlightRecorderPolicyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "flight-recorder-policy",
		Short:        "Manage the tenant's flight-recorder capture policy (tenant_admin)",
		Long:         "Read/write the operator's own tenant flight-recorder capture policy.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newSetCmd())
	return cmd
}

type setOptions struct {
	enabled           bool
	agentReadable     string
	retentionDays     int
	clearRetention    bool
	enabledSet        bool
	agentReadableSet  bool
	retentionDaysSet  bool
	jsonOut           bool
	backplaneOverride string
}

// newSetCmd returns `meho tenants flight-recorder-policy set`.
//
//	meho tenants flight-recorder-policy set
//	  [--enabled=true|false]              # F1 per-tenant capture default
//	  [--agent-readable=true|false|inherit] # F5 tri-state (inherit clears to null)
//	  [--retention-days N]                # F4 window (1..365)
//	  [--clear-retention]                 # clear retention back to the global default
//	  [--json] [--backplane <url>]
//
// Tenant-scoped (the caller's own tenant, from the JWT). tenant_admin only.
// Only the flags the operator actually set are sent (sparse PATCH), so an
// unset field is left unchanged — the tri-state null-vs-absent distinction.
//
// Exit codes: 0 ok; 2 auth_expired; 3 unreachable; 4 unexpected (incl. 422 /
// 404); 5 insufficient_role.
func newSetCmd() *cobra.Command {
	var opts setOptions
	cmd := &cobra.Command{
		Use:   "set",
		Short: "Update the tenant flight-recorder capture policy (tenant_admin)",
		Long: "set PATCHes /api/v1/tenants/flight-recorder-policy for the operator's own " +
			"tenant. tenant_admin only — operator / read_only land as 403 insufficient_role.\n\n" +
			"Only the flags you pass are sent; an omitted field is left unchanged. " +
			"--enabled flips the per-tenant capture default (F1). --agent-readable is " +
			"tri-state (F5): true / false force the agent-read override, inherit clears it " +
			"back to following the capture default. --retention-days sets the per-tenant " +
			"trace window (F4, 1..365 days); --clear-retention resets it to the global " +
			"default. Pass at least one field flag.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			opts.enabledSet = cmd.Flags().Changed("enabled")
			opts.agentReadableSet = cmd.Flags().Changed("agent-readable")
			opts.retentionDaysSet = cmd.Flags().Changed("retention-days")
			return runSet(cmd, opts)
		},
	}
	cmd.Flags().BoolVar(&opts.enabled, "enabled", false,
		"per-tenant capture default (F1); send true or false")
	cmd.Flags().StringVar(&opts.agentReadable, "agent-readable", "",
		"agent-read override (F5): true | false | inherit (inherit clears to the capture default)")
	cmd.Flags().IntVar(&opts.retentionDays, "retention-days", 0,
		"per-tenant trace retention window in days (F4; 1..365)")
	cmd.Flags().BoolVar(&opts.clearRetention, "clear-retention", false,
		"clear the retention override back to the global default")
	cmd.Flags().BoolVar(&opts.jsonOut, "json", false,
		"emit the resolved policy as JSON instead of the human summary")
	cmd.Flags().StringVar(&opts.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// buildBody assembles the sparse PATCH body from the flags the operator set.
// Returns an error for contradictory / empty / malformed flag combinations
// before any network call. A nil map value marshals to JSON null (an explicit
// clear); a key absent from the map is left unchanged server-side.
func buildBody(opts setOptions) (map[string]any, error) {
	body := map[string]any{}
	if opts.enabledSet {
		body["flight_recorder_enabled"] = opts.enabled
	}
	if opts.agentReadableSet {
		switch opts.agentReadable {
		case "true":
			body["flight_recorder_agent_readable"] = true
		case "false":
			body["flight_recorder_agent_readable"] = false
		case "inherit":
			body["flight_recorder_agent_readable"] = nil // explicit null -> clear
		default:
			return nil, fmt.Errorf(
				"--agent-readable must be one of: true, false, inherit; got %q", opts.agentReadable)
		}
	}
	if opts.retentionDaysSet && opts.clearRetention {
		return nil, fmt.Errorf("--retention-days and --clear-retention are mutually exclusive")
	}
	if opts.retentionDaysSet {
		if opts.retentionDays < 1 || opts.retentionDays > 365 {
			return nil, fmt.Errorf(
				"--retention-days must be between 1 and 365; got %d", opts.retentionDays)
		}
		body["flight_recorder_retention_days"] = opts.retentionDays
	}
	if opts.clearRetention {
		body["flight_recorder_retention_days"] = nil // explicit null -> global default
	}
	if len(body) == 0 {
		return nil, fmt.Errorf(
			"nothing to change; pass at least one of --enabled / --agent-readable / " +
				"--retention-days / --clear-retention")
	}
	return body, nil
}

func runSet(cmd *cobra.Command, opts setOptions) error {
	body, err := buildBody(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.jsonOut)
	}
	backplaneURL, err := backplane.Resolve(opts.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.jsonOut)
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("encode request body: %v", err)), opts.jsonOut)
	}
	resp, err := patchPolicy(cmd.Context(), backplaneURL, payload)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.jsonOut)
	}
	if resp.StatusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode, resp.Body, opts.jsonOut)
	}
	var policy api.TenantFlightRecorderPolicy
	if err := json.Unmarshal(resp.Body, &policy); err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("decode policy response: %v", err)), opts.jsonOut)
	}
	if opts.jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), policy)
	}
	printPolicySummary(cmd.OutOrStdout(), &policy)
	return nil
}

func patchPolicy(ctx context.Context, backplaneURL string, payload []byte) (*rawResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return doRequest(ctx, authed, func(ctx context.Context) (*http.Response, error) {
		return authed.UpdateFlightRecorderPolicyApiV1TenantsFlightRecorderPolicyPatchWithBody(
			ctx,
			&api.UpdateFlightRecorderPolicyApiV1TenantsFlightRecorderPolicyPatchParams{},
			"application/json",
			bytes.NewReader(payload),
		)
	})
}

// printPolicySummary renders the resolved policy as a compact confirmation.
// The nullable fields render "inherit" / "default (global)" for a NULL so the
// operator sees the tri-state resolution, not an empty cell.
func printPolicySummary(w io.Writer, p *api.TenantFlightRecorderPolicy) {
	if p == nil {
		return
	}
	agent := "inherit (follows capture default)"
	if p.FlightRecorderAgentReadable != nil {
		agent = fmt.Sprintf("%t", *p.FlightRecorderAgentReadable)
	}
	retention := "default (global)"
	if p.FlightRecorderRetentionDays != nil {
		retention = fmt.Sprintf("%d days", *p.FlightRecorderRetentionDays)
	}
	fmt.Fprintln(w, "updated flight-recorder policy")
	fmt.Fprintf(w, "%-16s %s\n", "tenant_id:", p.TenantId.String())
	fmt.Fprintf(w, "%-16s %t\n", "capture:", p.FlightRecorderEnabled)
	fmt.Fprintf(w, "%-16s %s\n", "agent-readable:", agent)
	fmt.Fprintf(w, "%-16s %s\n", "retention:", retention)
}
