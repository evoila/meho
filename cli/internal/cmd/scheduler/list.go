// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"context"
	"fmt"
	"io"
	"net/http"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListCmd returns the `meho scheduler list` command.
//
//	meho scheduler list [--kind K] [--status S] [--tenant T]
//	                    [--limit N] [--offset N] [--json] [--backplane <url>]
//
// Role: operator. Operator role is scoped to its own tenant; --tenant is
// a tenant_admin-only filter (the backend returns 403 for an operator
// who passes it).
func newListCmd() *cobra.Command {
	var (
		kind              string
		status            string
		workRef           string
		tenant            string
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List scheduled triggers in your tenant",
		Long: "list calls GET /api/v1/scheduler/triggers and renders " +
			"the triggers in the operator's tenant, newest-first. " +
			"--kind narrows to cron|one_off|event; --status narrows to " +
			"active|paused|cancelled|fired. --work-ref narrows to triggers " +
			"carrying that exact change-ticket reference (e.g. " +
			"gh:evoila/meho#13). --tenant is a tenant_admin-" +
			"only filter that targets another tenant; operator role " +
			"calling with --tenant lands as 403 insufficient_role. " +
			"--limit caps the page size (1..500, server default 100). " +
			"--offset advances the page window (default 0). --json emits " +
			"the raw ListResponse envelope for jq pipelines.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runList(cmd, listOptions{
				Kind:              kind,
				Status:            status,
				WorkRef:           workRef,
				Tenant:            tenant,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&kind, "kind", "",
		"filter by trigger kind: cron | one_off | event")
	cmd.Flags().StringVar(&status, "status", "",
		"filter by trigger status: active | paused | cancelled | fired")
	cmd.Flags().StringVar(&workRef, "work-ref", "",
		"filter by external change-ticket reference (exact match, e.g. gh:evoila/meho#13)")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"target tenant UUID (tenant_admin only; operator role is locked to its own tenant)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max triggers per page (1..500, server default 100 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"offset into the result set (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw ListResponse JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listOptions struct {
	Kind              string
	Status            string
	WorkRef           string
	Tenant            string
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runList(cmd *cobra.Command, opts listOptions) error {
	if opts.Kind != "" && !validKinds[opts.Kind] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--kind must be one of: cron, one_off, event"), opts.JSONOut)
	}
	if opts.Status != "" && !validStatuses[opts.Status] {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("--status must be one of: active, paused, cancelled, fired"),
			opts.JSONOut)
	}
	if opts.Limit < 0 || opts.Limit > 500 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 500; got %d", opts.Limit)),
			opts.JSONOut)
	}
	if opts.Offset < 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--offset must be non-negative; got %d", opts.Offset)),
			opts.JSONOut)
	}
	// Parse --tenant CLI-side so a malformed UUID surfaces locally
	// as a clear error rather than after the server's 422 round-trip.
	// The generated `TenantFilter` is `*openapi_types.UUID`; sending
	// raw strings is not an option on the typed client.
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
	resp, err := getList(cmd.Context(), backplaneURL, opts, tenantFilter)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil
	// (oapi-codegen's `ParseListTriggers...` only populates the
	// typed field when Content-Type contains `application/json`).
	// Without this guard, a malformed 200 would print "no scheduled
	// triggers in this tenant" as if the tenant genuinely had zero
	// — actively misleading. Mirrors the convention in
	// `cli/internal/cmd/status.go:142` and the post-iter-2 fix the
	// kb sibling adopted on PR #1282.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a scheduler list payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printListTable(cmd.OutOrStdout(), resp.JSON200)
	return nil
}

// listQueryParams maps the CLI flags onto the generated query-param
// shape. Mirrors the pre-migration `buildListPath` query-string
// composition exactly: --kind / --status are typed enum pointers;
// --tenant is a UUID pointer; --limit / --offset only send when
// non-zero so the backend's default-100 page-size kicks in when the
// operator omits the flag. The bearer header is injected by the
// `api.AuthedClient` request editor; the `Authorization` form param
// the generated `Params` struct exposes is left nil so we don't
// double-emit it.
func listQueryParams(opts listOptions, tenantFilter *openapi_types.UUID) *api.ListTriggersApiV1SchedulerTriggersGetParams {
	params := &api.ListTriggersApiV1SchedulerTriggersGetParams{}
	if opts.Kind != "" {
		k := api.ListTriggersApiV1SchedulerTriggersGetParamsKind(opts.Kind)
		params.Kind = &k
	}
	if opts.Status != "" {
		s := api.ListTriggersApiV1SchedulerTriggersGetParamsStatus(opts.Status)
		params.Status = &s
	}
	if opts.WorkRef != "" {
		wr := opts.WorkRef
		params.WorkRef = &wr
	}
	if tenantFilter != nil {
		params.TenantFilter = tenantFilter
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Offset > 0 {
		o := opts.Offset
		params.Offset = &o
	}
	return params
}

// getList calls GET /api/v1/scheduler/triggers via the generated
// typed client. The 401-refresh-retry loop runs through retryOn401.
// The authed-client construction is hoisted out of the retry loop
// so a credential failure routes directly to renderRequestError
// rather than getting swallowed by the retry shape (per the T11
// #1285 iter-2 lesson).
func getList(
	ctx context.Context,
	backplaneURL string,
	opts listOptions,
	tenantFilter *openapi_types.UUID,
) (*api.ListTriggersApiV1SchedulerTriggersGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := listQueryParams(opts, tenantFilter)
	return retryOn401(
		ctx,
		authed,
		func(ctx context.Context) (*api.ListTriggersApiV1SchedulerTriggersGetResponse, error) {
			return authed.ListTriggersApiV1SchedulerTriggersGetWithResponse(ctx, params)
		},
		func(r *api.ListTriggersApiV1SchedulerTriggersGetResponse) int { return r.StatusCode() },
	)
}

// printListTable renders the triggers as a compact table:
// ID, KIND, STATUS, SKIPS, SCHEDULE (cron_expr or fire_at), NEXT_FIRE_AT,
// WORK_REF. The SKIPS column (#2327) surfaces the consecutive-skip count so
// an `active` trigger that is silently skipping every tick no longer reads
// as healthy on `meho scheduler list`; "-" when the trigger is firing
// cleanly. Consumes `api.ScheduledTriggerListResponse` directly so the
// generated typed envelope is the single source of truth for the
// on-screen shape.
func printListTable(w io.Writer, r *api.ScheduledTriggerListResponse) {
	if r == nil || len(r.Triggers) == 0 {
		fmt.Fprintln(w, "no scheduled triggers in this tenant")
		return
	}
	fmt.Fprintf(w, "%-36s %-8s %-10s %-6s %-30s %-24s %s\n",
		"ID", "KIND", "STATUS", "SKIPS", "SCHEDULE", "NEXT_FIRE_AT", "WORK_REF")
	for _, t := range r.Triggers {
		schedule := ""
		switch string(t.Kind) {
		case "cron":
			if t.CronExpr != nil {
				schedule = *t.CronExpr
				if t.Timezone != "" && t.Timezone != "UTC" {
					schedule = schedule + " (" + t.Timezone + ")"
				}
			}
		case "one_off":
			if t.FireAt != nil {
				schedule = formatTime(t.FireAt)
			}
		case "event":
			schedule = "event-filter"
		}
		next := "-"
		if t.NextFireAt != nil {
			next = formatTime(t.NextFireAt)
		}
		workRef := "-"
		if t.WorkRef != nil && *t.WorkRef != "" {
			workRef = *t.WorkRef
		}
		skips := "-"
		if t.SkipCount > 0 {
			skips = fmt.Sprintf("%d", t.SkipCount)
		}
		fmt.Fprintf(w, "%-36s %-8s %-10s %-6s %-30s %-24s %s\n",
			t.Id.String(), string(t.Kind), string(t.Status), skips, schedule, next, workRef)
	}
}
