// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newHistoryCmd returns the `meho topology history` command.
//
//	meho topology history <node-name|alias> \
//	  [--node-kind K]              # disambiguate ambiguous name
//	  [--since DUR]                # 24h | 7d | 30m | ISO-8601 lower bound
//	  [--until DUR]                # same shorthand as --since upper bound
//	  [--include-edges]            # also walk incident edges' history
//	  [--limit N]                  # 1..5000, server default 5000
//	  [--json]                     # raw TopologyHistoryResult JSON
//	  [--backplane <url>]          # override the configured backplane
//
// Calls GET /api/v1/topology/history/{name}. The companion to
// `meho topology timeline`: timeline is "what changed in the graph
// at all"; history is "what changed for THIS specific resource".
// The default table view renders `valid_from / src / change / 1-line
// diff summary`; `--json` carries the full `snapshot.before` /
// `snapshot.after` JSONB per row so an operator can pipe into jq for
// forensic reconstruction.
//
// Exit codes (shared with the sibling topology verbs via
// renderRequestError / renderHTTPStatus):
//   - 0   query returned cleanly (incl. zero-row result).
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. 404 node_not_found and
//     409 ambiguous_node).
//   - 5   insufficient_role
func newHistoryCmd() *cobra.Command {
	var (
		nodeKind          string
		since             string
		until             string
		includeEdges      bool
		limit             int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "history <node-name|alias>",
		Short: "Walk the per-resource history of one node (G9.3 history)",
		Long: "history calls GET /api/v1/topology/history/<name> and " +
			"renders the chronological mutation log for one node " +
			"(and optionally its incident edges via --include-edges) " +
			"ordered newest-first. Unlike `meho topology timeline` " +
			"(tenant-wide feed, summary only), history is anchored on " +
			"one resource and carries the full snapshot.before / " +
			"snapshot.after JSONB per row -- the forensic shape for " +
			"'what was the exact state before this change?'. " +
			"--node-kind disambiguates when the bare name resolves to " +
			"multiple kinds. --since / --until accept duration " +
			"shorthand (24h / 7d / 30m / 2w) or ISO-8601 directly. " +
			"--json emits the raw TopologyHistoryResult; the table " +
			"view stays scannable.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runHistory(cmd, historyOptions{
				Name:              args[0],
				NodeKind:          nodeKind,
				Since:             since,
				Until:             until,
				IncludeEdges:      includeEdges,
				Limit:             limit,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&nodeKind, "node-kind", "",
		"pin the anchor to one node kind when the name is ambiguous across kinds")
	cmd.Flags().StringVar(&since, "since", "",
		"earliest valid_from; accepts 24h / 7d / 30m / 2w shorthand, RFC3339, or YYYY-MM-DD")
	cmd.Flags().StringVar(&until, "until", "",
		"latest valid_from; accepts the same shorthand as --since")
	cmd.Flags().BoolVar(&includeEdges, "include-edges", false,
		"also walk history rows for edges incident to the anchor node")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows returned (1..5000, server-side cap when omitted)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw TopologyHistoryResult JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// historyOptions is the per-call option bag for runHistory.
//
// Limit=0 sentinel: Go's int zero collides with the backend's ge=1
// validation. The CLI sends `--limit` only when explicitly set so the
// server applies its cap rather than erroring.
type historyOptions struct {
	Name              string
	NodeKind          string
	Since             string
	Until             string
	IncludeEdges      bool
	Limit             int
	JSONOut           bool
	BackplaneOverride string
}

// _historyLimitMax mirrors the API's Query(le=_HISTORY_LIMIT_MAX)
// ceiling so the CLI fails fast on an over-budget --limit instead of
// burning a round trip to a 422.
const _historyLimitMax = 5000

func runHistory(cmd *cobra.Command, opts historyOptions) error {
	if opts.Limit < 0 || opts.Limit > _historyLimitMax {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--limit must be between 1 and %d (or 0/omitted for the server default); got %d",
				_historyLimitMax, opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, statusCode, body, err := getHistory(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a history payload", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printHistoryTable(cmd.OutOrStdout(), opts.Name, result)
	return nil
}

// buildHistoryParams assembles the generated query-param shape. The
// anchor name is a path segment carried by the typed call's second
// arg, not a param. Optional filters land as pointer fields only when
// set so the server applies its defaults for the rest. Duration
// shorthand (e.g. "24h") is resolved client-side to an absolute
// time.Time the typed param then carries — one parser in one place
// (the CLI), matching the timeline contract.
func buildHistoryParams(opts historyOptions, now time.Time) (*api.HistoryRouteApiV1TopologyHistoryNameGetParams, error) {
	params := &api.HistoryRouteApiV1TopologyHistoryNameGetParams{}
	if opts.NodeKind != "" {
		k := opts.NodeKind
		params.Kind = &k
	}
	if opts.Since != "" {
		ts, err := resolveDurationOrISO(opts.Since, now)
		if err != nil {
			return nil, fmt.Errorf("--since %q: %w", opts.Since, err)
		}
		params.Since = &ts
	}
	if opts.Until != "" {
		ts, err := resolveDurationOrISO(opts.Until, now)
		if err != nil {
			return nil, fmt.Errorf("--until %q: %w", opts.Until, err)
		}
		params.Until = &ts
	}
	if opts.IncludeEdges {
		ie := true
		params.IncludeEdges = &ie
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	return params, nil
}

func getHistory(
	ctx context.Context,
	backplaneURL string,
	opts historyOptions,
) (*api.TopologyHistoryResult, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	params, err := buildHistoryParams(opts, time.Now().UTC())
	if err != nil {
		return nil, 0, nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.HistoryRouteApiV1TopologyHistoryNameGetResponse, error) {
			return authed.HistoryRouteApiV1TopologyHistoryNameGetWithResponse(ctx, opts.Name, params)
		},
		func(r *api.HistoryRouteApiV1TopologyHistoryNameGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, 0, nil, err
	}
	return resp.JSON200, resp.StatusCode(), resp.Body, nil
}

// printHistoryTable renders the per-resource history page as a
// compact, scannable table. Columns: VALID_FROM, SRC, CHANGE,
// SUMMARY, AUDIT_ID. The summary is derived client-side from
// snapshot.before / snapshot.after; the table view does not show
// the full snapshot (that is the `--json` mode's job, the forensic
// payload an operator pipes into `jq`). An empty result renders the
// "no changes" line.
func printHistoryTable(w io.Writer, root string, r *api.TopologyHistoryResult) {
	if r == nil || len(r.Rows) == 0 {
		fmt.Fprintf(w, "no history rows for %q in this tenant (or window is empty)\n", root)
		return
	}
	fmt.Fprintf(w, "%-22s %-5s %-8s %-38s %s\n",
		"VALID_FROM", "SRC", "CHANGE", "SUMMARY", "AUDIT_ID")
	for _, row := range r.Rows {
		fmt.Fprintf(w, "%-22s %-5s %-8s %-38s %s\n",
			truncate(row.ValidFrom.UTC().Format(time.RFC3339), 22),
			row.Source,
			truncate(row.ChangeKind, 8),
			truncate(historyRowSummary(row), 38),
			truncate(uuidPtrToString(row.AuditId), 36),
		)
	}
	fmt.Fprintf(w, "anchor: %s; include_edges: %t; rows: %d\n",
		r.AnchorNodeId, r.IncludeEdges, len(r.Rows))
}

// historyRowSummary renders a 1-line description of a history row
// for the table view. Picks the post-state for created / updated
// (the row as it exists after the mutation -- more useful for
// "what's new" surveys) and the pre-state for removed (the row that
// just went away). Falls back to "<change_kind> <source>" when the
// snapshot is malformed or missing.
//
// `row.Snapshot` is `*map[string]interface{}` in the generated
// client (Pydantic JSONB rendered as a pointer to a map so absence
// is representable). Dereference once before the switch / lookup
// below to keep the field access cheap and readable.
func historyRowSummary(row api.TopologyHistoryEntry) string {
	if row.Snapshot == nil {
		return fmt.Sprintf("%s %s", row.ChangeKind, row.Source)
	}
	snap := *row.Snapshot
	var side any
	if row.ChangeKind == "removed" {
		side = snap["before"]
	} else {
		side = snap["after"]
	}
	m, ok := side.(map[string]any)
	if !ok {
		return fmt.Sprintf("%s %s", row.ChangeKind, row.Source)
	}
	if row.Source == "node" {
		kind, _ := m["kind"].(string)
		name, _ := m["name"].(string)
		if kind == "" {
			kind = "node"
		}
		if name == "" {
			name = "<unknown>"
		}
		return fmt.Sprintf("%s %s %s", row.ChangeKind, kind, name)
	}
	edgeKind, _ := m["kind"].(string)
	if edgeKind == "" {
		edgeKind = "edge"
	}
	return fmt.Sprintf("%s %s", row.ChangeKind, edgeKind)
}
