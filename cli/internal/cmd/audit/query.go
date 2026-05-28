// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

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
//	  [--session-id ID]            # narrow to one agent session (UUID)
//	  [--json]                     # raw AuditQueryResult JSON
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
		sessionID         string
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
			"prior page. --json emits the raw AuditQueryResult so " +
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
				SessionID:         sessionID,
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
	cmd.Flags().StringVar(&sessionID, "session-id", "",
		"narrow to one agent session (UUID); the flat companion to `meho audit replay`")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AuditQueryResult JSON instead of the human table")
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
	SessionID         string
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
	// UUIDs live in the generated request body as
	// `*openapi_types.UUID`; parse the operator strings at the verb
	// edge so the bad-input case is a clean output.Unexpected rather
	// than a server-side 422 after a round-trip. Empty string means
	// "filter not set" and stays nil on the wire.
	body, err := buildAuditQueryRequest(opts)
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(err.Error()),
			opts.JSONOut,
		)
	}
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
		// Emit the server bytes verbatim so payload integers above
		// 2^53 survive without rounding through the generated
		// `map[string]interface{}` decoder. The human-rendered
		// summary lossily accepts the float64 conversion for the
		// table/summary view; the --json path is the precision-
		// preserving contract callers pipe through jq.
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditQueryResult"),
			opts.JSONOut,
		)
	}
	printQueryTable(cmd.OutOrStdout(), result)
	return nil
}

// buildAuditQueryRequest assembles the typed `api.AuditQueryRequest`
// from per-call options. Filter fields land on the wire as nil
// (omitted-or-null per the field's JSON tag) when the operator
// didn't set them — the backend's Pydantic model treats absent /
// null filters as "no narrowing on this column". UUIDs are parsed
// at the edge so an obviously-malformed value short-circuits before
// a network round-trip.
func buildAuditQueryRequest(opts queryOptions) (api.AuditQueryRequest, error) {
	body := api.AuditQueryRequest{}
	if opts.Target != "" {
		v := opts.Target
		body.Target = &v
	}
	if opts.Principal != "" {
		v := opts.Principal
		body.Principal = &v
	}
	if opts.OpID != "" {
		v := opts.OpID
		body.OpId = &v
	}
	if opts.OpClass != "" {
		v := opts.OpClass
		body.OpClass = &v
	}
	if opts.ResultStatus != "" {
		v := opts.ResultStatus
		body.ResultStatus = &v
	}
	if opts.Since != "" {
		v := opts.Since
		body.Since = &v
	}
	if opts.Until != "" {
		v := opts.Until
		body.Until = &v
	}
	if opts.Cursor != "" {
		v := opts.Cursor
		body.Cursor = &v
	}
	if opts.AuditID != "" {
		u, perr := uuid.Parse(strings.TrimSpace(opts.AuditID))
		if perr != nil {
			return body, fmt.Errorf(
				"--audit-id must be a valid UUID; %q is not a UUID", opts.AuditID)
		}
		uu := openapi_types.UUID(u)
		body.AuditId = &uu
	}
	if opts.ParentAuditID != "" {
		u, perr := uuid.Parse(strings.TrimSpace(opts.ParentAuditID))
		if perr != nil {
			return body, fmt.Errorf(
				"--parent-audit-id must be a valid UUID; %q is not a UUID", opts.ParentAuditID)
		}
		uu := openapi_types.UUID(u)
		body.ParentAuditId = &uu
	}
	if opts.SessionID != "" {
		u, perr := uuid.Parse(strings.TrimSpace(opts.SessionID))
		if perr != nil {
			return body, fmt.Errorf(
				"--session-id must be a valid UUID; %q is not a UUID", opts.SessionID)
		}
		uu := openapi_types.UUID(u)
		body.AgentSessionId = &uu
	}
	if opts.Limit > 0 {
		l := opts.Limit
		body.Limit = &l
	}
	return body, nil
}

// postQuery drives the typed-client `QueryApiV1AuditQueryPost`
// endpoint with a one-shot 401-retry around the underlying
// AuthedClient's refresh path (mirrors `AuthedClient.GetHealth`'s
// pattern in client.go). Non-2xx responses are returned as
// `*httpResponseError` so the caller can route them through
// `renderHTTPStatus`; transport-layer errors return verbatim.
// Returns both the raw response body bytes (for --json verbatim
// passthrough) and the decoded typed envelope (for the table render).
func postQuery(
	ctx context.Context,
	client *api.AuthedClient,
	body api.AuditQueryRequest,
) ([]byte, *api.AuditQueryResult, error) {
	resp, err := client.QueryApiV1AuditQueryPostWithResponse(ctx, nil, body)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.QueryApiV1AuditQueryPostWithResponse(ctx, nil, body)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}

// printQueryTable renders the audit page as a compact, scannable
// table. Columns: TIME, PRINCIPAL, TARGET, OP_ID, CLASS, STATUS per
// the issue body's spec. When `next_cursor` is set, a final NEXT
// line tells the operator how to paste-paginate.
func printQueryTable(w io.Writer, r *api.AuditQueryResult) {
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
			truncate(formatTS(row.Ts), 22),
			truncate(row.PrincipalSub, 12),
			truncate(target, 18),
			truncate(row.OpId, 26),
			truncate(row.OpClass, 16),
			truncate(row.ResultStatus, 8),
		)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		fmt.Fprintf(w, "NEXT: --cursor=%s  (paste to continue)\n", *r.NextCursor)
	}
}
