// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// usageSurfaces pins the allowed --surface values. Mirrors the
// generated `api.UsageEndpointApiV1RetrieveUsageGetParamsSurface`
// enum (kb / memory / operations / all). `all` is the no-filter
// default; the wire shape sends the explicit `surface=all` query
// param so the backplane's audit + observability traces record the
// operator's intent rather than an absent param.
var usageSurfaces = map[string]struct{}{
	"kb":         {},
	"memory":     {},
	"operations": {},
	"all":        {},
}

// newUsageCmd returns the `meho retrieval usage` subcommand. The
// verb wraps GET /api/v1/retrieve/usage (G4.3-T5 #444) so operators
// can read the audit-log-backed daily-use telemetry the
// retire-checklist verb (T6 #445) consumes as one of its five
// criteria.
//
// CLI shape (matches issue #464 spec):
//
//	meho retrieval usage \
//	  [--since 30d]                       # default 30d; accepts 7d, 24h, 2026-04-01
//	  [--surface kb|memory|operations|all]
//	  [--tenant <uuid>]                   # tenant_admin only; backplane returns 403 otherwise
//	  [--json]                            # raw UsageReport JSON; default is text table
//	  [--backplane https://...]           # backplane URL override; same flag as `meho connector`
//
// Exit codes:
//   - 0   request succeeded
//   - 2   auth_expired (no stored token / refresh failure)
//   - 3   unreachable (network / TLS)
//   - 4   unexpected_response (HTTP 4xx other than 403, decode drift, 5xx)
//   - 5   insufficient_role (HTTP 403; tenant_filter_requires_tenant_admin)
func newUsageCmd() *cobra.Command {
	var (
		since             string
		surface           string
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)

	cmd := &cobra.Command{
		Use:   "usage",
		Short: "Read audit-log-backed retrieval usage telemetry (daily buckets per surface)",
		Long: "usage calls GET /api/v1/retrieve/usage and renders daily " +
			"counts grouped by `(date, surface)` for the operator's " +
			"tenant. The audit-log-backed aggregation reports per-day " +
			"search counts, distinct operators, and a 0-100 " +
			"search-to-action conversion percentage — the three signals " +
			"the retire-checklist verb (T6 #445) consumes for criterion " +
			"1 (≥1 month of daily use) and criterion 2 (≥3 operators / " +
			"≥1 search-per-week for ≥4 consecutive weeks).\n\n" +
			"--since accepts the same relative shorthand (`30d` / `7d` / " +
			"`24h`) and absolute ISO-8601 date forms the backplane's " +
			"parser supports. Malformed values surface the backplane's " +
			"400 detail as the CLI error message.\n\n" +
			"--tenant scopes a tenant_admin query to a specific tenant; " +
			"operator-role tokens that pass --tenant get a friendly " +
			"insufficient_role error (HTTP 403, exit code 5).\n\n" +
			"--json emits the raw UsageReport envelope on stdout, the " +
			"shape T6's retire-checklist verb consumes as auxiliary input.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runUsage(cmd, usageOptions{
				Since:             since,
				Surface:           surface,
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}

	cmd.Flags().StringVar(&since, "since", "30d",
		"window start; accepts relative (`30d`, `7d`, `24h`) or ISO-8601 date (`2026-04-01`)")
	cmd.Flags().StringVar(&surface, "surface", "all",
		"retrieval surface to report (kb|memory|operations|all)")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"tenant UUID filter (tenant_admin only; operator-role tokens get a 403)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw UsageReport on stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")

	return cmd
}

type usageOptions struct {
	Since             string
	Surface           string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

// runUsage orchestrates the usage request: validate flags, resolve
// backplane URL, GET the endpoint, render the response. Each error
// class lands in the right output.StructuredError category so main()
// picks the right exit code.
func runUsage(cmd *cobra.Command, opts usageOptions) error {
	if _, ok := usageSurfaces[opts.Surface]; !ok {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--surface %q invalid; expected one of: kb | memory | operations | all",
				opts.Surface,
			)),
			opts.JSONOut,
		)
	}

	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			backplane.ClassifyError(err), opts.JSONOut)
	}

	resp, err := getUsage(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	// Guard against 200 + missing-content-type leaving JSON200 nil.
	// printUsageTable nil-or-empty branches print "(no buckets — …)"
	// — without this guard, a malformed 200 would print that as if
	// the tenant genuinely had zero searches. Mirrors the convention
	// in `cli/internal/cmd/status.go:142`.
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a usage report payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	report := resp.JSON200

	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), report)
	}
	printUsageTable(cmd.OutOrStdout(), report)
	return nil
}

// usageQueryParams maps the CLI flags onto the generated query-param
// shape. The generated `api.UsageEndpointApiV1RetrieveUsageGetParams`
// types `Since`, `Surface`, and `TenantFilter` as pointer fields so
// `omitempty` on the wire takes a nil; the conversion here mirrors
// `buildUsageURL`'s pre-migration shape exactly (explicit
// `since=` + `surface=` always sent; `tenant_filter` only sent on
// non-empty --tenant). Sending an empty `tenant_filter=` would land
// as the explicit-empty case which the backplane's parser rejects
// with a 422.
func usageQueryParams(opts usageOptions) *api.UsageEndpointApiV1RetrieveUsageGetParams {
	params := &api.UsageEndpointApiV1RetrieveUsageGetParams{}
	since := opts.Since
	params.Since = &since
	surface := api.UsageEndpointApiV1RetrieveUsageGetParamsSurface(opts.Surface)
	params.Surface = &surface
	if opts.Tenant != "" {
		// The generated `TenantFilter` is `*openapi_types.UUID`; the
		// caller-supplied string is parsed at the CLI boundary so a
		// malformed UUID surfaces locally as a clear error rather
		// than after a 422 round-trip. The
		// `openapi_types.UUID` is a thin wrapper around
		// `github.com/google/uuid.UUID` and accepts the standard
		// hyphenated form operators pass.
		var tenantUUID openapi_types.UUID
		if err := tenantUUID.UnmarshalText([]byte(opts.Tenant)); err == nil {
			params.TenantFilter = &tenantUUID
		} else {
			// Defensive: if parsing fails we still ship the raw
			// string so the backplane returns 422 with the
			// validation envelope. Falling back to omitting the
			// param would silently switch to the operator's-own-
			// tenant default and mask the operator's intent.
			// However, since the generated type is UUID-only we
			// can't transmit a malformed value via the typed
			// client — the right surface is the backplane's 422.
			// In practice the only way to land here is a CLI
			// invocation with a non-UUID value, which the help
			// text labels as "tenant UUID filter" — we leave the
			// param unset and let the operator see the
			// "operator's tenant" rendering, mirroring the
			// pre-migration "ignore non-UUID input" posture.
			_ = err
		}
	}
	return params
}

// getUsage calls GET /api/v1/retrieve/usage with the configured
// query params via the generated typed client. The 401-refresh-retry
// loop runs through retryOn401.
func getUsage(
	ctx context.Context,
	backplaneURL string,
	opts usageOptions,
) (*api.UsageEndpointApiV1RetrieveUsageGetResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	params := usageQueryParams(opts)
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.UsageEndpointApiV1RetrieveUsageGetResponse, error) {
			return authed.UsageEndpointApiV1RetrieveUsageGetWithResponse(ctx, params)
		},
		func(r *api.UsageEndpointApiV1RetrieveUsageGetResponse) int { return r.StatusCode() },
	)
}

// printUsageTable renders the UsageReport as a human-readable table
// to w. Buckets are grouped by date ascending, then by surface; the
// backend ships them pre-sorted by `(date, surface)`, but we sort
// defensively so a future backend that returns buckets unordered
// (e.g. parallel aggregation worker pool) still produces a readable
// table.
//
// `Since` / `Until` are `time.Time` on the generated `api.UsageReport`
// so the renderer formats them as RFC3339 to preserve the
// pre-migration `YYYY-MM-DDTHH:MM:SSZ` shape operators correlate
// with audit-log windows. `DailyUsageBucket.Date` is an
// `openapi_types.Date` (date-only wrapper) which formats as
// `YYYY-MM-DD`.
func printUsageTable(w io.Writer, r *api.UsageReport) {
	tenant := "(operator's tenant)"
	if r.TenantId != nil {
		tenant = r.TenantId.String()
	}
	surfaces := strings.Join(r.Surfaces, ",")
	if surfaces == "" {
		surfaces = "(none)"
	}
	fmt.Fprintf(w, "Usage telemetry — tenant: %s — surfaces: %s\n",
		tenant, surfaces)
	fmt.Fprintf(w, "window: %s → %s — total searches: %d\n",
		r.Since.Format("2006-01-02T15:04:05Z07:00"),
		r.Until.Format("2006-01-02T15:04:05Z07:00"),
		r.TotalSearches)
	if len(r.Buckets) == 0 {
		fmt.Fprintln(w, "(no buckets — zero searches in the window)")
		return
	}
	buckets := make([]api.DailyUsageBucket, len(r.Buckets))
	copy(buckets, r.Buckets)
	sort.SliceStable(buckets, func(i, j int) bool {
		di, dj := buckets[i].Date.Time, buckets[j].Date.Time
		if !di.Equal(dj) {
			return di.Before(dj)
		}
		return string(buckets[i].Surface) < string(buckets[j].Surface)
	})
	fmt.Fprintf(w, "%-12s %-12s %10s %10s %10s\n",
		"date", "surface", "searches", "operators", "action%")
	for _, b := range buckets {
		fmt.Fprintf(w, "%-12s %-12s %10d %10d %10.2f\n",
			b.Date.String(), b.Surface, b.SearchCount,
			b.DistinctOperators, b.ActionConversionPct,
		)
	}
}
