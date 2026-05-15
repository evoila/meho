// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// auditQueryRequest mirrors the backend `AuditQueryRequest` Pydantic
// model (`backend/src/meho_backplane/api/v1/audit_models.py`). Every
// filter field is a pointer (or `omitempty`-tagged primitive) so the
// CLI emits only the keys the operator actually set — the backend
// treats absent keys as "no narrowing on this column". `Limit=0`
// special-cases the unset state because Go's zero-value int collides
// with the backend's `ge=1` validation otherwise.
type auditQueryRequest struct {
	Target        *string `json:"target,omitempty"`
	Principal     *string `json:"principal,omitempty"`
	OpID          *string `json:"op_id,omitempty"`
	OpClass       *string `json:"op_class,omitempty"`
	ResultStatus  *string `json:"result_status,omitempty"`
	Since         *string `json:"since,omitempty"`
	Until         *string `json:"until,omitempty"`
	AuditID       *string `json:"audit_id,omitempty"`
	ParentAuditID *string `json:"parent_audit_id,omitempty"`
	Limit         int     `json:"limit,omitempty"`
	Cursor        *string `json:"cursor,omitempty"`
}

// newQueryCmd returns the `meho audit query` command.
//
// CLI shape (matches issue #467 spec):
//
//	meho audit query \
//	  [--target T]                 # name or alias (server resolves)
//	  [--principal P]              # operator sub or partial match
//	  [--op-id PATTERN]            # glob: vsphere.vm.*
//	  [--op-class C]               # read | write | credential_read | audit_query | other
//	  [--result-status S]          # ok | error | denied
//	  [--since DUR]                # 24h | 7d | ISO-8601
//	  [--until DUR]
//	  [--limit N]                  # 1..1000, default 100 (server-side)
//	  [--cursor C]                 # opaque forward-pagination cursor
//	  [--audit-id ID]              # exact-id lookup
//	  [--parent-audit-id ID]       # v0.2 substrate rejects (400)
//	  [--json]                     # raw RetireChecklistReport JSON
//	  [--backplane <url>]          # override the configured backplane
//
// Exit codes:
//   - 0   query returned cleanly (incl. zero-row result).
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. 400 parser errors,
//     unsupported filter, invalid cursor; 404 from show only).
//   - 5   insufficient_role
func newQueryCmd() *cobra.Command {
	var (
		target            string
		principal         string
		opID              string
		opClass           string
		resultStatus      string
		since             string
		until             string
		limit             int
		cursor            string
		auditID           string
		parentAuditID     string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "query",
		Short: "Query the audit log with arbitrary filter combinations",
		Long: "query calls POST /api/v1/audit/query and renders the audit " +
			"rows matching the operator-supplied filters. Tenant scoping " +
			"is enforced server-side via the JWT — there is no surface " +
			"that accepts a tenant id. --since/--until accept either a " +
			"duration shorthand (24h / 7d / 30m / 2w) or an ISO-8601 " +
			"datetime. --cursor pastes the opaque next_cursor from a " +
			"prior page. --json emits the raw QueryResult so " +
			"operators can pipe into jq.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runQuery(cmd, queryOptions{
				Target:            target,
				Principal:         principal,
				OpID:              opID,
				OpClass:           opClass,
				ResultStatus:      resultStatus,
				Since:             since,
				Until:             until,
				Limit:             limit,
				Cursor:            cursor,
				AuditID:           auditID,
				ParentAuditID:     parentAuditID,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&target, "target", "",
		"narrow to one target (name or alias; server-side resolution)")
	cmd.Flags().StringVar(&principal, "principal", "",
		"narrow to one operator (JWT subject; partial-match supported)")
	cmd.Flags().StringVar(&opID, "op-id", "",
		"narrow to one op-id (glob with * wildcards)")
	cmd.Flags().StringVar(&opClass, "op-class", "",
		"narrow to one op-class (read|write|credential_read|audit_query|other)")
	cmd.Flags().StringVar(&resultStatus, "result-status", "",
		"narrow to one result-status (ok|error|denied)")
	cmd.Flags().StringVar(&since, "since", "",
		"earliest occurred_at; accepts 24h / 7d / 30m / 2w shorthand or ISO-8601")
	cmd.Flags().StringVar(&until, "until", "",
		"latest occurred_at; accepts the same shorthand as --since")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows per page (1..1000, server default 100 when omitted)")
	cmd.Flags().StringVar(&cursor, "cursor", "",
		"opaque forward-pagination cursor from a prior page's NEXT line")
	cmd.Flags().StringVar(&auditID, "audit-id", "",
		"exact audit-id lookup (UUID)")
	cmd.Flags().StringVar(&parentAuditID, "parent-audit-id", "",
		"narrow to the composite-op subtree under this audit-id "+
			"(v0.2 substrate rejects with 400; column lands with G0.6-T7 #398)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw QueryResult JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type queryOptions struct {
	Target            string
	Principal         string
	OpID              string
	OpClass           string
	ResultStatus      string
	Since             string
	Until             string
	Limit             int
	Cursor            string
	AuditID           string
	ParentAuditID     string
	JSONOut           bool
	BackplaneOverride string
}

func runQuery(cmd *cobra.Command, opts queryOptions) error {
	if opts.Limit < 0 || opts.Limit > 1000 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be between 1 and 1000; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := postQuery(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printQueryTable(cmd.OutOrStdout(), result)
	return nil
}

// buildQueryRequest assembles the JSON body from the per-call
// options. Each filter field lands on the wire only when the operator
// set it. Empty strings stay empty (Pydantic's `extra="ignore"`
// silently drops fields not on the model; the explicit Go-side
// omission keeps the wire compact and matches the existing CLI
// pattern in `cli/internal/cmd/retrieval/eval.go`).
func buildQueryRequest(opts queryOptions) auditQueryRequest {
	body := auditQueryRequest{}
	if opts.Target != "" {
		body.Target = &opts.Target
	}
	if opts.Principal != "" {
		body.Principal = &opts.Principal
	}
	if opts.OpID != "" {
		body.OpID = &opts.OpID
	}
	if opts.OpClass != "" {
		body.OpClass = &opts.OpClass
	}
	if opts.ResultStatus != "" {
		body.ResultStatus = &opts.ResultStatus
	}
	if opts.Since != "" {
		body.Since = &opts.Since
	}
	if opts.Until != "" {
		body.Until = &opts.Until
	}
	if opts.AuditID != "" {
		body.AuditID = &opts.AuditID
	}
	if opts.ParentAuditID != "" {
		body.ParentAuditID = &opts.ParentAuditID
	}
	if opts.Cursor != "" {
		body.Cursor = &opts.Cursor
	}
	if opts.Limit > 0 {
		body.Limit = opts.Limit
	}
	return body
}

func postQuery(ctx context.Context, backplaneURL string, opts queryOptions) (*QueryResult, error) {
	body := buildQueryRequest(opts)
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal audit query: %w", err)
	}
	resp, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/audit/query", raw)
	if err != nil {
		return nil, err
	}
	var out QueryResult
	if err := decodeAuditResponse(resp, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// printQueryTable renders the audit page as a compact, scannable
// table. Columns: TIME, PRINCIPAL, TARGET, OP_ID, CLASS, STATUS per
// the issue body's spec. When `next_cursor` is set, a final NEXT
// line tells the operator how to paste-paginate.
func printQueryTable(w io.Writer, r *QueryResult) {
	if r == nil || len(r.Rows) == 0 {
		fmt.Fprintln(w, "no audit rows matched the filter")
		return
	}
	fmt.Fprintf(w, "%-22s %-12s %-18s %-26s %-16s %s\n",
		"TIME", "PRINCIPAL", "TARGET", "OP_ID", "CLASS", "STATUS")
	for _, row := range r.Rows {
		target := strDeref(row.TargetName)
		if target == "" {
			target = "-"
		}
		fmt.Fprintf(w, "%-22s %-12s %-18s %-26s %-16s %s\n",
			truncate(row.TS, 22),
			truncate(row.PrincipalSub, 12),
			truncate(target, 18),
			truncate(row.OpID, 26),
			truncate(row.OpClass, 16),
			truncate(row.ResultStatus, 8),
		)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		fmt.Fprintf(w, "NEXT: --cursor=%s  (paste to continue)\n", *r.NextCursor)
	}
}
