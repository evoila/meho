// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// UsageBucket mirrors the backend DailyUsageBucket model (audit-log
// aggregation row, one per `(date, surface)` pair). Hand-written
// rather than reused from the oapi-codegen output: the generated
// types use `openapi_types.Date` for the `date` field, which adds an
// import for every test that constructs fixtures. Plain string keeps
// the wire shape (ISO-8601 YYYY-MM-DD) directly addressable. Field
// names match the JSON keys the backend ships verbatim — drift here
// would break the `--json` round-trip the retire-checklist verb
// (T6 #445) relies on as auxiliary input.
type UsageBucket struct {
	Date                string  `json:"date"`
	Surface             string  `json:"surface"`
	SearchCount         int     `json:"search_count"`
	DistinctOperators   int     `json:"distinct_operators"`
	ActionConversionPct float64 `json:"action_conversion_pct"`
}

// UsageReport mirrors the backend `UsageReport` envelope returned by
// GET /api/v1/retrieve/usage. `TenantID` is a `*string` — the
// backend ships JSON `null` for the cross-tenant placeholder shape,
// and consumers (notably the retire-checklist verb the issue body
// names as the downstream of `--json`) key off the explicit-null
// presence rather than the field's absence. `omitempty` would drop
// the key on round-trip and break that shape.
type UsageReport struct {
	Since         string        `json:"since"`
	Until         string        `json:"until"`
	Surfaces      []string      `json:"surfaces"`
	TenantID      *string       `json:"tenant_id"`
	Buckets       []UsageBucket `json:"buckets"`
	TotalSearches int           `json:"total_searches"`
}

// usageSurfaces pins the allowed --surface values. Mirrors the
// backend's `UsageEndpointApiV1RetrieveUsageGetParamsSurface` enum
// (kb / memory / operations / all). `all` is the no-filter default;
// the wire shape sends the explicit `surface=all` query param so the
// backplane's audit + observability traces record the operator's
// intent rather than an absent param.
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

	report, err := getUsage(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderUsageError(cmd, backplaneURL, err, opts.JSONOut)
	}

	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), report)
	}
	printUsageTable(cmd.OutOrStdout(), report)
	return nil
}

// getUsage calls GET /api/v1/retrieve/usage with the configured
// query params and decodes the JSON response into a UsageReport.
// Mirrors `postEval`'s 401-retry shape: one transparent refresh +
// retry on auth failure.
func getUsage(
	ctx context.Context,
	backplaneURL string,
	opts usageOptions,
) (*UsageReport, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errors.New("meho: stored token has no access_token")
	}

	target := buildUsageURL(backplaneURL, opts)

	resp, err := getUsageWithBearer(ctx, httpClient, target, bearer)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		// One-shot refresh + retry, mirroring api.AuthedClient.GetHealth
		// and the sibling eval / retire-checklist runners.
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close()
			return nil, rerr
		}
		resp.Body.Close()
		bearer = authed.AccessToken()
		resp, err = getUsageWithBearer(ctx, httpClient, target, bearer)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// 4 KiB cap on the error body: the FastAPI default 400 / 403
		// payload is tens of bytes; nothing legitimate runs into
		// kilobytes here. The cap defends against a pathological /
		// hostile backplane shipping a megabyte error body.
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, &usageHTTPError{
			StatusCode: resp.StatusCode,
			Body:       strings.TrimSpace(string(raw)),
		}
	}

	var out UsageReport
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode usage response: %w", err)
	}
	return &out, nil
}

// buildUsageURL composes the GET URL with query params for the usage
// endpoint. Pulled out for testability: the query-param shape (param
// names, encoding of tenant UUIDs with hyphens, omission of empty
// tenant) is load-bearing for the wire contract with T5's backplane
// route and best tested directly.
//
// The query-param names mirror the backend route signature
// (`since` / `surface` / `tenant_filter`). The `tenant_filter` param
// is omitted entirely when --tenant is unset so the backplane's
// "operator's own tenant" default fires; passing an empty
// `tenant_filter=` would land as the explicit-empty case which the
// backplane's parser rejects with a 422.
func buildUsageURL(backplaneURL string, opts usageOptions) string {
	q := url.Values{}
	q.Set("since", opts.Since)
	q.Set("surface", opts.Surface)
	if opts.Tenant != "" {
		q.Set("tenant_filter", opts.Tenant)
	}
	return backplaneURL + "/api/v1/retrieve/usage?" + q.Encode()
}

// getUsageWithBearer issues the actual GET request with the supplied
// bearer token. Split out so the 401-refresh-retry path can reuse the
// same URL string without re-building it.
func getUsageWithBearer(
	ctx context.Context,
	client *http.Client,
	target, bearer string,
) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, fmt.Errorf("build usage request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/json")
	return client.Do(req)
}

// usageHTTPError carries a non-2xx response so renderUsageError can
// pick the right output.StructuredError category (403 →
// insufficient_role; other 4xx/5xx → unexpected_response). Pattern
// mirrors the connector sibling's httpError; duplicated here rather
// than imported because cmd/connector ↔ cmd/retrieval would import-
// cycle through cmd/root.go's wire-up.
type usageHTTPError struct {
	StatusCode int
	Body       string
}

func (e *usageHTTPError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// renderUsageError translates an error from getUsage into the right
// output.RenderError category. Adds two branches over the package's
// shared renderRequestError: a `usageHTTPError` 403 lands as
// insufficient_role (exit 5) so an operator-role token passing
// --tenant gets the "ask tenant_admin for the role grant" hint, and
// any other non-2xx (notably 400 for a malformed --since) lands as
// unexpected_response with the backplane's detail string surfaced
// verbatim so the operator sees the actionable backend hint.
//
// Kept local to usage.go (rather than promoted onto the package-
// level renderRequestError) because the eval + retire-checklist
// verbs route every >=400 status to "unexpected" via the generic
// HTTP-error string wrap they emit today; promoting the role-aware
// classification onto the shared helper would change their error
// shape too. Keeping the divergence narrow until a follow-up audit
// can roll the eval / retire-checklist verbs onto the same typed-
// error path.
func renderUsageError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	var he *usageHTTPError
	if errors.As(err, &he) {
		if he.StatusCode == http.StatusForbidden {
			return output.RenderError(cmd.ErrOrStderr(),
				output.InsufficientRole(fmt.Sprintf(
					"call %s: HTTP 403: %s (this verb's --tenant flag requires tenant_admin role)",
					backplaneURL, he.Body,
				)),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP %d: %s", backplaneURL, he.StatusCode, he.Body,
			)),
			jsonOut,
		)
	}
	// Decode failures (contract drift between T5's backplane and the
	// CLI's UsageReport shape) classify as unexpected — the request
	// reached the backplane and the backplane returned 200; the body
	// just didn't match the agreed shape. Without this split, a
	// regression would present to the operator as "your network is
	// down", which is misleading.
	var syntaxErr *json.SyntaxError
	var unmarshalErr *json.UnmarshalTypeError
	if errors.As(err, &syntaxErr) ||
		errors.As(err, &unmarshalErr) ||
		errors.Is(err, io.ErrUnexpectedEOF) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: invalid JSON response: %v", backplaneURL, err,
			)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// printUsageTable renders the UsageReport as a human-readable table
// to w. Buckets are grouped by date ascending, then by surface; the
// backend ships them pre-sorted by `(date, surface)`, but we sort
// defensively so a future backend that returns buckets unordered
// (e.g. parallel aggregation worker pool) still produces a readable
// table.
func printUsageTable(w io.Writer, r *UsageReport) {
	tenant := "(operator's tenant)"
	if r.TenantID != nil && *r.TenantID != "" {
		tenant = *r.TenantID
	}
	surfaces := strings.Join(r.Surfaces, ",")
	if surfaces == "" {
		surfaces = "(none)"
	}
	fmt.Fprintf(w, "Usage telemetry — tenant: %s — surfaces: %s\n",
		tenant, surfaces)
	fmt.Fprintf(w, "window: %s → %s — total searches: %d\n",
		r.Since, r.Until, r.TotalSearches)
	if len(r.Buckets) == 0 {
		fmt.Fprintln(w, "(no buckets — zero searches in the window)")
		return
	}
	buckets := make([]UsageBucket, len(r.Buckets))
	copy(buckets, r.Buckets)
	sort.SliceStable(buckets, func(i, j int) bool {
		if buckets[i].Date != buckets[j].Date {
			return buckets[i].Date < buckets[j].Date
		}
		return buckets[i].Surface < buckets[j].Surface
	})
	fmt.Fprintf(w, "%-12s %-12s %10s %10s %10s\n",
		"date", "surface", "searches", "operators", "action%")
	for _, b := range buckets {
		fmt.Fprintf(w, "%-12s %-12s %10d %10d %10.2f\n",
			b.Date, b.Surface, b.SearchCount,
			b.DistinctOperators, b.ActionConversionPct,
		)
	}
}
