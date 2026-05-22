// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"time"

	"github.com/spf13/cobra"

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
// renderRequestError / renderHTTPError):
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
		"earliest valid_from; accepts 24h / 7d / 30m / 2w shorthand or ISO-8601")
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
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := getTimeline(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printTimelineTable(cmd.OutOrStdout(), result)
	return nil
}

// buildTimelinePath assembles the GET path + query string for a
// timeline call. Optional filters land as query params only when set
// so the server applies its defaults for the rest. Duration shorthand
// (e.g. "24h") is resolved client-side to an absolute ISO-8601 to
// match the REST router's absolute-only contract — the audit-query
// router parses shorthand server-side but the topology timeline route
// (a fresh G9.3 surface) keeps shorthand on the CLI to minimise the
// backend's parsing surface (one parser, in one place: the CLI). The
// raw input falls through as-is if it doesn't parse as shorthand —
// the operator may already have pasted an ISO-8601.
func buildTimelinePath(opts timelineOptions, now time.Time) (string, error) {
	q := url.Values{}
	if opts.Target != "" {
		q.Set("target", opts.Target)
	}
	if opts.Since != "" {
		iso, err := resolveDurationOrISO(opts.Since, now)
		if err != nil {
			return "", fmt.Errorf("--since %q: %w", opts.Since, err)
		}
		q.Set("since", iso)
	}
	if opts.Until != "" {
		iso, err := resolveDurationOrISO(opts.Until, now)
		if err != nil {
			return "", fmt.Errorf("--until %q: %w", opts.Until, err)
		}
		q.Set("until", iso)
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Cursor != "" {
		q.Set("cursor", opts.Cursor)
	}
	path := "/api/v1/topology/timeline"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path, nil
}

func getTimeline(ctx context.Context, backplaneURL string, opts timelineOptions) (*TimelineResult, error) {
	path, err := buildTimelinePath(opts, time.Now().UTC())
	if err != nil {
		return nil, err
	}
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", path, nil)
	if err != nil {
		return nil, err
	}
	var out TimelineResult
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode timeline response: %w", err)
	}
	return &out, nil
}

// TimelineEntry mirrors the backend `TopologyTimelineEntry` Pydantic
// model (`backend/src/meho_backplane/topology/schemas.py`). Fields are
// hand-written rather than oapi-codegen-generated for the same reason
// the audit `Entry` shape is — the topology package stays decoupled
// from oapi churn, matching the sibling-package convention.
type TimelineEntry struct {
	ValidFrom   string  `json:"valid_from"`
	HistoryID   int64   `json:"history_id"`
	Source      string  `json:"source"`
	ChangeKind  string  `json:"change_kind"`
	ResourceID  *string `json:"resource_id"`
	Summary     string  `json:"summary"`
	AuditID     *string `json:"audit_id"`
}

// TimelineResult mirrors the backend `TopologyTimelineResult`.
// `NextCursor` keeps the JSON key as `null` (no `omitempty`) so a
// re-marshal preserves the Pydantic wire shape — the audit
// QueryResult does the same.
type TimelineResult struct {
	Rows       []TimelineEntry `json:"rows"`
	NextCursor *string         `json:"next_cursor"`
}

// printTimelineTable renders the timeline page as a compact,
// scannable table. Columns: VALID_FROM, SRC, CHANGE, SUMMARY,
// AUDIT_ID. When `next_cursor` is set, a final NEXT line tells the
// operator how to paste-paginate.
func printTimelineTable(w io.Writer, r *TimelineResult) {
	if r == nil || len(r.Rows) == 0 {
		fmt.Fprintln(w, "no graph changes in the requested window")
		return
	}
	fmt.Fprintf(w, "%-22s %-5s %-8s %-38s %s\n",
		"VALID_FROM", "SRC", "CHANGE", "SUMMARY", "AUDIT_ID")
	for _, row := range r.Rows {
		fmt.Fprintf(w, "%-22s %-5s %-8s %-38s %s\n",
			truncate(row.ValidFrom, 22),
			row.Source,
			truncate(row.ChangeKind, 8),
			truncate(row.Summary, 38),
			truncate(strDeref(row.AuditID), 36),
		)
	}
	if r.NextCursor != nil && *r.NextCursor != "" {
		fmt.Fprintf(w, "NEXT: --cursor=%s  (paste to continue)\n", *r.NextCursor)
	}
}

// strDeref returns *s or empty string when s is nil. Mirrors the
// audit-package helper of the same name; duplicated to avoid the
// import-cycle the cmd/audit → cmd/topology → cmd path would create.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// resolveDurationOrISO converts an operator-typed --since/--until
// value into an absolute RFC3339 timestamp. Accepts:
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
func resolveDurationOrISO(raw string, now time.Time) (string, error) {
	if iso, ok := tryParseDuration(raw, now); ok {
		return iso, nil
	}
	parsed, err := time.Parse(time.RFC3339, raw)
	if err == nil {
		return parsed.UTC().Format(time.RFC3339), nil
	}
	// One more shape: bare ISO-8601 date (no time component). Promote
	// to RFC3339 midnight UTC so the server's `valid_from >= :since`
	// compare lands somewhere meaningful for an operator typing a
	// date.
	parsed, err = time.Parse("2006-01-02", raw)
	if err == nil {
		return parsed.UTC().Format(time.RFC3339), nil
	}
	return "", fmt.Errorf(
		"not a duration shorthand (e.g. 24h) or ISO-8601 timestamp",
	)
}

// tryParseDuration recognises <N><unit> shorthand. unit ∈ {s,m,h,d,w};
// N is an unsigned integer ≤ 9999.
func tryParseDuration(raw string, now time.Time) (string, bool) {
	if len(raw) < 2 {
		return "", false
	}
	unit := raw[len(raw)-1]
	digits := raw[:len(raw)-1]
	if len(digits) == 0 || len(digits) > 4 {
		return "", false
	}
	n, err := strconv.Atoi(digits)
	if err != nil || n < 0 {
		return "", false
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
		return "", false
	}
	return now.Add(-dur).UTC().Format(time.RFC3339), true
}
