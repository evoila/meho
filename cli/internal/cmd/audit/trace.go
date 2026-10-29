// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newTraceCmd returns the `meho audit trace` group (#4855). The flight
// recorder is MEHO's short-lived record of the vendor calls one
// operation made. The group sits under `audit` because the trace
// route is an audit route and uses the same audit id as
// `meho audit show`.
//
//	meho audit trace list [--op-id GLOB] [--target NAME] [--since DUR]
//	                      [--until DUR] [--limit N] [--cursor C] [--json]
//	meho audit trace show <audit-id> [--json]
func newTraceCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "trace",
		Short: "List the operation runs that have a stored flight-recorder trace, or read one trace",
		Long: "The flight recorder keeps a short-lived record of the vendor " +
			"calls one operation made: method, URL, status, time, and " +
			"redacted headers and bodies. MEHO deletes a trace when the " +
			"tenant's keep time ends (7 days by default; a tenant admin can " +
			"set 1 to 365 days), so these verbs only see traces that are " +
			"still stored. `list` finds the operation runs with a trace; " +
			"`show` prints one trace by its audit id.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newTraceListCmd())
	cmd.AddCommand(newTraceShowCmd())
	return cmd
}

// newTraceListCmd returns `meho audit trace list`. It is `meho audit
// query` with `has_trace: true` fixed on the request body and a
// table that puts the audit id first, because `show` needs it.
//
// Exit codes match `meho audit query`: 0 clean (also for zero rows),
// 2 auth_expired, 3 unreachable, 4 unexpected response (bad flag
// value, 400 from the parser), 5 insufficient_role.
func newTraceListCmd() *cobra.Command {
	var opts traceListOptions
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List the operation runs that still have a stored trace",
		Long: "list calls POST /api/v1/audit/query with has_trace=true and " +
			"prints one line per operation run that still has a stored " +
			"flight-recorder trace, audit id first. MEHO deletes a trace " +
			"when the tenant's keep time ends (7 days by default), so a " +
			"run older than that is not listed even though its audit row " +
			"still exists. --op-id, --target, --since, --until, --limit and " +
			"--cursor mean the same as on `meho audit query`. --json prints " +
			"the route's answer unchanged.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTraceList(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.OpID, "op-id", "",
		"narrow to one op-id (glob with * wildcards, e.g. 'vmware.*')")
	cmd.Flags().StringVar(&opts.Target, "target", "",
		"narrow to one target (name or alias; server-side resolution)")
	cmd.Flags().StringVar(&opts.Since, "since", "",
		"earliest run time; accepts 24h / 7d / 30m / 2w shorthand or ISO-8601")
	cmd.Flags().StringVar(&opts.Until, "until", "",
		"latest run time; accepts the same shorthand as --since")
	cmd.Flags().IntVar(&opts.Limit, "limit", 0,
		"max rows per page (1..1000, server default 100 when omitted)")
	cmd.Flags().StringVar(&opts.Cursor, "cursor", "",
		"opaque forward-pagination cursor from a prior page's NEXT line")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false,
		"print the route's AuditQueryResult JSON unchanged instead of the table")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type traceListOptions struct {
	OpID              string
	Target            string
	Since             string
	Until             string
	Limit             int
	Cursor            string
	JSONOut           bool
	BackplaneOverride string
}

func runTraceList(cmd *cobra.Command, opts traceListOptions) error {
	if opts.Limit < 0 || opts.Limit > 1000 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 1000; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	body, err := buildAuditQueryRequest(queryOptions{
		Target: opts.Target,
		OpID:   opts.OpID,
		Since:  opts.Since,
		Until:  opts.Until,
		Limit:  opts.Limit,
		Cursor: opts.Cursor,
	})
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	hasTrace := true
	body.HasTrace = &hasTrace

	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	rawBody, result, err := postQuery(cmd.Context(), client, body)
	if err != nil {
		return routeRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditQueryResult"),
			opts.JSONOut,
		)
	}
	printTraceListTable(cmd.OutOrStdout(), result)
	return nil
}

// printTraceListTable renders one line per operation run with a stored
// trace. The audit id comes first so an operator can paste it into
// `meho audit trace show`. TENANT is the tenant name, or the tenant id
// when the backend has no name for it (#5238). It is not
// `printQueryTable` because this table drops the PRINCIPAL and CLASS
// columns, which say nothing about whether a trace exists.
func printTraceListTable(w io.Writer, r *api.AuditQueryResult) {
	if r == nil || len(r.Rows) == 0 {
		fmt.Fprintln(w, "no stored traces matched the filter")
		return
	}
	fmt.Fprintf(w, "%-36s  %-22s %-18s %-18s %-32s %s\n",
		"AUDIT ID", "TIME", "TENANT", "TARGET", "OP_ID", "STATUS")
	for _, row := range r.Rows {
		target := strDeref(row.TargetName)
		if target == "" {
			target = "-"
		}
		fmt.Fprintf(w, "%-36s  %-22s %-18s %-18s %-32s %s\n",
			row.Id.String(),
			truncate(formatTS(row.Ts), 22),
			truncate(tenantLabel(row.TenantName, row.TenantId), 18),
			truncate(target, 18),
			truncate(row.OpId, 32),
			truncate(row.ResultStatus, 8),
		)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		fmt.Fprintf(w, "NEXT: --cursor=%s  (paste to continue)\n", *r.NextCursor)
	}
}
