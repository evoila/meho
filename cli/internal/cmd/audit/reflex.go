// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"fmt"
	"io"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newReflexCmd returns the `meho audit reflex` command (#3134). The
// verb wraps GET /api/v1/audit/reflex so operators can read the
// reflex-adoption KPIs the backplane computes over `audit_log` +
// the announce store: read-before-act, announce coverage, and
// write-back rate, each split by surface (agent vs CLI/REST).
//
// CLI shape:
//
//	meho audit reflex \
//	  [--since 7d]                        # default 7d; accepts 30d, 24h, 2026-08-01
//	  [--until 1d]                        # window upper bound; default now
//	  [--tenant <uuid>]                   # platform_admin only; backplane returns 403 otherwise
//	  [--json]                            # raw ReflexReport JSON; default is a text table
//	  [--backplane https://...]           # backplane URL override
//
// Exit codes mirror `meho audit query`.
func newReflexCmd() *cobra.Command {
	var (
		since             string
		until             string
		tenant            string
		jsonOut           bool
		backplaneOverride string
	)

	cmd := &cobra.Command{
		Use:   "reflex",
		Short: "Read reflex-adoption KPIs (read-before-act / announce coverage / write-back)",
		Long: "reflex calls GET /api/v1/audit/reflex and renders the " +
			"reflex-adoption KPIs the backplane aggregates from the " +
			"audit log and the announce store, split by surface (agent " +
			"vs CLI/REST):\n\n" +
			"  - read-before-act: %% of agent sessions whose first " +
			"call_operation is preceded by a broadcast_recent.\n" +
			"  - announce coverage: %% of write-class operations executed " +
			"with an earlier same-session announce claim.\n" +
			"  - write-back rate: add_to_knowledge + add_to_memory calls " +
			"per 100 call_operation calls.\n\n" +
			"--since / --until accept the same relative shorthand (`7d` / " +
			"`24h`) and absolute ISO-8601 date forms the audit surface " +
			"supports; --until defaults to now. Ratios read `n/a` when " +
			"their denominator is zero (e.g. the CLI/REST surface has no " +
			"agent session, so the client-side reflex metrics are N/A " +
			"there). --tenant scopes a platform_admin query to a specific " +
			"tenant; other tokens get a 403. --json emits the raw " +
			"ReflexReport envelope.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runReflex(cmd, reflexOptions{
				Since:             since,
				Until:             until,
				Tenant:            tenant,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}

	cmd.Flags().StringVar(&since, "since", "7d",
		"window start; accepts relative (`7d`, `24h`) or ISO-8601 date (`2026-08-01`)")
	cmd.Flags().StringVar(&until, "until", "",
		"window end; same grammar as --since; defaults to now when omitted")
	cmd.Flags().StringVar(&tenant, "tenant", "",
		"tenant UUID filter (platform_admin only; other tokens get a 403)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw ReflexReport on stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")

	return cmd
}

type reflexOptions struct {
	Since             string
	Until             string
	Tenant            string
	JSONOut           bool
	BackplaneOverride string
}

// runReflex orchestrates the request: resolve backplane URL, GET the
// endpoint, render the response.
func runReflex(cmd *cobra.Command, opts reflexOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	rawBody, report, err := getReflex(cmd.Context(), client, opts)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if report == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against ReflexReport"),
			opts.JSONOut,
		)
	}
	printReflexReport(cmd.OutOrStdout(), report)
	return nil
}

// buildReflexParams maps the CLI flags onto the generated query-param
// shape. The query-param fields are pointer-typed so nil means "don't
// emit on the wire"; the backend then applies its own defaults
// (since=7d, until=now). --since is always sent (it carries a CLI
// default). A malformed --tenant is left unset so the backend's own
// tenant default renders, mirroring `meho retrieval usage`.
func buildReflexParams(opts reflexOptions) *api.ReflexEndpointApiV1AuditReflexGetParams {
	params := &api.ReflexEndpointApiV1AuditReflexGetParams{}
	since := opts.Since
	params.Since = &since
	if opts.Until != "" {
		until := opts.Until
		params.Until = &until
	}
	if opts.Tenant != "" {
		var tenantUUID openapi_types.UUID
		if err := tenantUUID.UnmarshalText([]byte(opts.Tenant)); err == nil {
			params.TenantFilter = &tenantUUID
		}
	}
	return params
}

// getReflex drives the typed-client endpoint with the same one-shot
// 401-retry shape the sibling audit verbs use.
func getReflex(
	ctx context.Context,
	client *api.AuthedClient,
	opts reflexOptions,
) ([]byte, *api.ReflexReport, error) {
	params := buildReflexParams(opts)
	resp, err := client.ReflexEndpointApiV1AuditReflexGetWithResponse(ctx, params)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.ReflexEndpointApiV1AuditReflexGetWithResponse(ctx, params)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}

// printReflexReport renders the ReflexReport as a human-readable table.
// Each surface gets one block with the three metrics and their raw
// numerator/denominator. A nil ratio (zero denominator) renders `n/a`.
func printReflexReport(w io.Writer, r *api.ReflexReport) {
	tenant := "(operator's tenant)"
	if r.TenantId != nil {
		tenant = r.TenantId.String()
	}
	fmt.Fprintf(w, "Reflex adoption — tenant: %s\n", tenant)
	fmt.Fprintf(w, "window: %s → %s\n",
		r.Since.Format("2006-01-02T15:04:05Z07:00"),
		r.Until.Format("2006-01-02T15:04:05Z07:00"),
	)
	for i := range r.Surfaces {
		s := r.Surfaces[i]
		fmt.Fprintf(w, "\n[%s]\n", s.Surface)
		fmt.Fprintf(w, "  read-before-act:  %s  (%d/%d sessions read-first)\n",
			pctOrNA(s.ReadBeforeActPct), s.ReadBeforeActReadFirst, s.ReadBeforeActSessions)
		fmt.Fprintf(w, "  announce-coverage:%s  (%d/%d write ops announced)\n",
			pctOrNA(s.AnnounceCoveragePct), s.AnnounceCoverageAnnounced, s.AnnounceCoverageWriteOps)
		fmt.Fprintf(w, "  write-back rate:  %s  (%d adds / %d call_operation)\n",
			rateOrNA(s.WriteBackPer100CallOps), s.WriteBackAddCalls, s.WriteBackCallOperations)
	}
}

// pctOrNA renders a nullable percentage as `NN.NN%` or `n/a` (nil).
func pctOrNA(v *float32) string {
	if v == nil {
		return "   n/a"
	}
	return fmt.Sprintf("%6.2f%%", *v)
}

// rateOrNA renders a nullable per-100 rate as `NN.NN` or `n/a` (nil).
func rateOrNA(v *float32) string {
	if v == nil {
		return "   n/a"
	}
	return fmt.Sprintf("%6.2f", *v)
}
