// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newTimelineCmd returns the `meho topology timeline` command.
//
//	meho topology timeline \
//	  [--target <name|alias>]      # narrow to one target's resources
//	  [--since DUR]                # 24h | 7d | 30m | ISO-8601 lower bound
//	  [--until DUR]                # same shorthand as --since upper bound
//	  [--limit N]                  # 1..1000, default 50 (server-side)
//	  [--cursor C]                 # opaque forward-pagination cursor
//	  [--json]                     # raw TopologyTimelineResult JSON
//	  [--backplane <url>]          # override the configured backplane
//
// Calls GET /api/v1/topology/timeline. Cursor pagination: a non-empty
// `next_cursor` line in the table view means more rows exist — paste it
// onto the next call as --cursor. Cursor is stable under concurrent
// inserts from the G9.3 diff-on-write hook (T2 #857): a new history
// row landing between page N and page N+1 is naturally placed by the
// keyset compare and never duplicates or skips a returned row.
//
// Exit codes (shared with the sibling topology verbs via
// renderRequestError / renderHTTPStatus):
//   - 0   query returned cleanly (incl. zero-row result).
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected response shape (incl. 400 invalid_cursor and 404
//     target_not_found with near-miss list).
//   - 5   insufficient_role
func newTimelineCmd() *cobra.Command {
	var (
		target            string
		since             string
		until             string
		limit             int
		cursor            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "timeline",
		Short: "Walk the tenant timeline of graph changes (G9.3 history)",
		Long: "timeline calls GET /api/v1/topology/timeline and renders the " +
			"chronological feed of `graph_node_history` + " +
			"`graph_edge_history` rows ordered newest-first. --target " +
			"narrows to history rows for one target's resources " +
			"(alias-aware; server-side resolution). --since / --until " +
			"accept either duration shorthand (24h / 7d / 30m / 2w) " +
			"resolved client-side to an absolute ISO-8601 timestamp, " +
			"or an ISO-8601 datetime directly. --cursor pastes the " +
			"opaque next_cursor from a prior page; the cursor is " +
			"stable under concurrent diff-on-write inserts so a paged " +
			"sweep over an actively-mutating graph reassembles to the " +
			"unpaged result with no gaps or duplicates. --json emits " +
			"the raw TopologyTimelineResult so operators can pipe into " +
			"jq.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runTimeline(cmd, timelineOptions{
				Target:            target,
				Since:             since,
				Until:             until,
				Limit:             limit,
				Cursor:            cursor,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&target, "target", "",
		"narrow to one target (name or alias; server-side resolution)")
	cmd.Flags().StringVar(&since, "since", "",
		"earliest valid_from; accepts 24h / 7d / 30m / 2w shorthand, RFC3339, or YYYY-MM-DD")
	cmd.Flags().StringVar(&until, "until", "",
		"latest valid_from; accepts the same shorthand as --since")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max rows per page (1..1000, server default 50 when omitted)")
	cmd.Flags().StringVar(&cursor, "cursor", "",
		"opaque forward-pagination cursor from a prior page's NEXT line")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw TopologyTimelineResult JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// timelineOptions is the per-call option bag for runTimeline.
//
// Limit=0 sentinel: Go's int zero collides with the backend's ge=1
// validation. The CLI sends `--limit` only when explicitly set so the
// server applies its 50-default rather than erroring.
type timelineOptions struct {
	Target            string
	Since             string
	Until             string
	Limit             int
	Cursor            string
	JSONOut           bool
	BackplaneOverride string
}

// _timelineLimitMax mirrors the API's Query(le=_TIMELINE_LIMIT_MAX)
// ceiling so the CLI fails fast on an over-budget --limit instead of
// burning a round trip to a 422.
const _timelineLimitMax = 1000

func runTimeline(cmd *cobra.Command, opts timelineOptions) error {
	if opts.Limit < 0 || opts.Limit > _timelineLimitMax {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--limit must be between 1 and %d (or 0/omitted for the server default of 50); got %d",
				_timelineLimitMax, opts.Limit)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, statusCode, body, err := getTimeline(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a timeline payload", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printTimelineTable(cmd.OutOrStdout(), result)
	return nil
}

// buildTimelineParams assembles the generated query-param shape for a
// timeline call. Optional filters land as pointer fields only when
// set so the server applies its defaults for the rest. Duration
// shorthand (e.g. "24h") is resolved client-side to an absolute
// time.Time the typed param then carries — keeps the shorthand vocab
// out of the backend's parser surface (one parser, in one place: the
// CLI).
func buildTimelineParams(opts timelineOptions, now time.Time) (*api.TimelineRouteApiV1TopologyTimelineGetParams, error) {
	params := &api.TimelineRouteApiV1TopologyTimelineGetParams{}
	if opts.Target != "" {
		t := opts.Target
		params.Target = &t
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
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Cursor != "" {
		c := opts.Cursor
		params.Cursor = &c
	}
	return params, nil
}

func getTimeline(
	ctx context.Context,
	backplaneURL string,
	opts timelineOptions,
) (*api.TopologyTimelineResult, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	params, err := buildTimelineParams(opts, time.Now().UTC())
	if err != nil {
		return nil, 0, nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.TimelineRouteApiV1TopologyTimelineGetResponse, error) {
			return authed.TimelineRouteApiV1TopologyTimelineGetWithResponse(ctx, params)
		},
		func(r *api.TimelineRouteApiV1TopologyTimelineGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, 0, nil, err
	}
	return resp.JSON200, resp.StatusCode(), resp.Body, nil
}

// printTimelineTable renders the timeline page as a compact,
// scannable table. Columns: VALID_FROM, SRC, CHANGE, SUMMARY,
// AUDIT_ID. When `next_cursor` is set, a final NEXT line tells the
// operator how to paste-paginate.
//
// `row.ValidFrom` is `time.Time` in the generated client; the table
// renders the RFC3339 form (matching the pre-migration string column).
func printTimelineTable(w io.Writer, r *api.TopologyTimelineResult) {
	if r == nil || len(r.Rows) == 0 {
		fmt.Fprintln(w, "no graph changes in the requested window")
		return
	}
	fmt.Fprintf(w, "%-22s %-5s %-8s %-38s %s\n",
		"VALID_FROM", "SRC", "CHANGE", "SUMMARY", "AUDIT_ID")
	for _, row := range r.Rows {
		fmt.Fprintf(w, "%-22s %-5s %-8s %-38s %s\n",
			truncate(row.ValidFrom.UTC().Format(time.RFC3339), 22),
			row.Source,
			truncate(row.ChangeKind, 8),
			truncate(row.Summary, 38),
			truncate(uuidPtrToString(row.AuditId), 36),
		)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		fmt.Fprintf(w, "NEXT: --cursor=%s  (paste to continue)\n", *r.NextCursor)
	}
}

// uuidPtrToString returns the canonical 8-4-4-4-12 form of a UUID
// pointer or the empty string when nil. Replaces the pre-migration
// strDeref helper (which worked on `*string`) for the generated
// client's `*openapi_types.UUID` audit / resource id fields.
func uuidPtrToString(u *openapi_types.UUID) string {
	if u == nil {
		return ""
	}
	return u.String()
}

// resolveDurationOrISO converts an operator-typed --since/--until
// value into an absolute time.Time. Accepts:
//
//   - duration shorthand: <N><unit> where unit ∈ {s,m,h,d,w}. Result
//     is `now - duration`.
//   - ISO-8601 / RFC3339: passed through after a parse-validate
//     round trip so a typo (`2026-13-32`) surfaces here, not as a
//     400 from the backend.
//
// Mirrors the audit-query duration parser
// (`backend/src/meho_backplane/audit_query/duration.py`) so the two
// surfaces accept the same vocabulary. Audit forensics often wants
// sub-minute resolution (`30s`) and multi-week windows (`2w`), so
// the timeline parser ships the wider grammar.
func resolveDurationOrISO(raw string, now time.Time) (time.Time, error) {
	if ts, ok := tryParseDuration(raw, now); ok {
		return ts, nil
	}
	parsed, err := time.Parse(time.RFC3339, raw)
	if err == nil {
		return parsed.UTC(), nil
	}
	// One more shape: bare ISO-8601 date (no time component). Promote
	// to midnight UTC so the server's `valid_from >= :since` compare
	// lands somewhere meaningful for an operator typing a date.
	parsed, err = time.Parse("2006-01-02", raw)
	if err == nil {
		return parsed.UTC(), nil
	}
	return time.Time{}, fmt.Errorf(
		"not a duration shorthand (e.g. 24h) or ISO-8601 timestamp",
	)
}

// tryParseDuration recognises <N><unit> shorthand. unit ∈ {s,m,h,d,w};
// N is an unsigned integer ≤ 9999.
func tryParseDuration(raw string, now time.Time) (time.Time, bool) {
	if len(raw) < 2 {
		return time.Time{}, false
	}
	unit := raw[len(raw)-1]
	digits := raw[:len(raw)-1]
	if len(digits) == 0 || len(digits) > 4 {
		return time.Time{}, false
	}
	n, err := strconv.Atoi(digits)
	if err != nil || n < 0 {
		return time.Time{}, false
	}
	var dur time.Duration
	switch unit {
	case 's':
		dur = time.Duration(n) * time.Second
	case 'm':
		dur = time.Duration(n) * time.Minute
	case 'h':
		dur = time.Duration(n) * time.Hour
	case 'd':
		dur = time.Duration(n) * 24 * time.Hour
	case 'w':
		dur = time.Duration(n) * 7 * 24 * time.Hour
	default:
		return time.Time{}, false
	}
	return now.Add(-dur).UTC(), true
}
