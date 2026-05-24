// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// defaultReplayMaxDepth is the depth past which the ASCII tree is
// truncated client-side. The replay route itself does not accept a
// depth parameter (its 413 count-first guard caps *rows*, not depth),
// so --max-depth is purely a rendering knob: nodes deeper than the
// limit are folded into a single "… N more level(s) (use --max-depth
// to expand)" marker. 20 matches the issue's spec.
const defaultReplayMaxDepth = 20

// ReplayNode mirrors the backend `ReplayNode` Pydantic model
// (`backend/src/meho_backplane/audit_query/schemas.py`), which
// subclasses `AuditEntry` and adds `depth` + `children`. Like `Entry`
// the fields are hand-written rather than aliased to the generated
// `api.ReplayNode` so the audit package stays decoupled from
// oapi-codegen churn — the same stance the rest of this package takes.
//
// Only the rendering-relevant fields are pulled out as named members;
// every other audit column survives the --json round-trip because the
// verb re-marshals the raw server bytes verbatim rather than this
// struct (see runReplay's --json branch). `DurationMS` is a `*string`
// because Pydantic v2 serialises `Decimal` as a quoted decimal string
// (or null).
type ReplayNode struct {
	TS           string       `json:"ts"`
	OpID         string       `json:"op_id"`
	ResultStatus string       `json:"result_status"`
	DurationMS   *string      `json:"duration_ms"`
	Depth        int          `json:"depth"`
	Children     []ReplayNode `json:"children"`
}

// ReplayResult mirrors the backend `AuditReplayResult` envelope — a
// `ReplayNode` forest plus the echoed session/tenant identity and the
// session's anchor-row count.
type ReplayResult struct {
	Root      []ReplayNode `json:"root"`
	SessionID string       `json:"session_id"`
	TenantID  string       `json:"tenant_id"`
	RowCount  int          `json:"row_count"`
}

// newReplayCmd returns the `meho audit replay` command.
//
// CLI shape:
//
//	meho audit replay <session-id> [--json] [--max-depth N] [--backplane <url>]
//
// Calls GET /api/v1/audit/sessions/{session_id}/replay and renders the
// session's parent/child audit tree. The default rendering is an ASCII
// tree (one line per node); --json emits the raw `AuditReplayResult`
// envelope verbatim (the v0.2.next compliance-export contract).
//
// A 413 from the backend means the session exceeds the server's
// 10 000-anchor-row replay cap. The verb surfaces a friendly redirect
// to `meho audit query --session-id <id>` (which paginates the flat
// rows) and exits non-zero — see renderHTTPError's 413 arm in audit.go.
//
// Exit codes:
//   - 0   tree rendered cleanly (incl. an empty / unknown session)
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 413 session_too_large, 422
//     malformed-UUID, and the client-side non-UUID rejection)
//   - 5   insufficient_role
func newReplayCmd() *cobra.Command {
	var (
		jsonOut           bool
		maxDepth          int
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "replay <session-id>",
		Short: "Replay one agent session as a parent/child audit tree",
		Long: "replay calls GET /api/v1/audit/sessions/{session_id}/replay " +
			"and renders the session's audit rows as a parent/child tree. " +
			"The argument must be a UUID (validated client-side). The " +
			"default output is an ASCII tree — one line per node, children " +
			"indented under their parent, roots in chronological order. " +
			"--json emits the raw AuditReplayResult JSON for piping into " +
			"jq or a compliance export. --max-depth folds nodes below the " +
			"given depth (rendering only; the server caps on row count, " +
			"not depth). A session larger than the server's 10 000-row cap " +
			"returns 413 and the verb redirects to `meho audit query " +
			"--session-id <id>`.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runReplay(cmd, replayOptions{
				SessionID:         args[0],
				JSONOut:           jsonOut,
				MaxDepth:          maxDepth,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw AuditReplayResult JSON instead of the human ASCII tree")
	cmd.Flags().IntVar(&maxDepth, "max-depth", defaultReplayMaxDepth,
		"fold tree nodes deeper than this level (rendering only; default 20)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type replayOptions struct {
	SessionID         string
	JSONOut           bool
	MaxDepth          int
	BackplaneOverride string
}

func runReplay(cmd *cobra.Command, opts replayOptions) error {
	if _, err := uuid.Parse(strings.TrimSpace(opts.SessionID)); err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"replay requires a valid UUID <session-id>; %q is not a UUID", opts.SessionID)),
			opts.JSONOut,
		)
	}
	if opts.MaxDepth < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--max-depth must be >= 0; got %d", opts.MaxDepth)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	raw, err := doAuthedRequest(
		cmd.Context(), backplaneURL, "GET", buildReplayPath(opts.SessionID), nil)
	if err != nil {
		return renderReplayRequestError(cmd, backplaneURL, opts.SessionID, err, opts.JSONOut)
	}
	if opts.JSONOut {
		// Emit the server bytes verbatim so every audit column on each
		// node survives the round-trip — the compliance-export contract
		// must not be lossy through the CLI's render-only struct.
		_, werr := cmd.OutOrStdout().Write(append(raw, '\n'))
		return werr
	}
	var result ReplayResult
	if derr := decodeAuditResponse(raw, &result); derr != nil {
		return renderRequestError(cmd, backplaneURL, derr, opts.JSONOut)
	}
	printReplayTree(cmd.OutOrStdout(), &result, opts.MaxDepth)
	return nil
}

// buildReplayPath assembles the GET path. Exposed for unit tests so the
// URL encoding of the session id stays covered.
func buildReplayPath(sessionID string) string {
	return "/api/v1/audit/sessions/" + pathEscape(sessionID) + "/replay"
}

// renderReplayRequestError adds the 413 session-too-large redirect on
// top of the shared error ladder. A 413 carries the session's row count
// in its `{detail: {detail: "session_too_large", row_count: N}}` body;
// the verb turns it into an actionable `meho audit query --session-id`
// pointer (the query verb paginates the flat rows the over-cap tree
// can't render). Every other status falls through to the shared
// renderRequestError ladder used by the sibling verbs.
func renderReplayRequestError(
	cmd *cobra.Command,
	backplaneURL, sessionID string,
	err error,
	jsonOut bool,
) error {
	var he *httpError
	if errors.As(err, &he) && he.StatusCode == http.StatusRequestEntityTooLarge {
		rows := decodeSessionTooLargeRowCount(he.Body)
		msg := fmt.Sprintf(
			"session %s has %s rows (cap %d); use: meho audit query --session-id %s",
			sessionID, rows, replayRowCap, sessionID)
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), jsonOut)
	}
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// printReplayTree renders the replay forest as an ASCII tree. Roots are
// emitted in chronological order (the server already orders both roots
// and children by (occurred_at, id)); each child is indented under its
// parent with the standard `├──`/`└──` connectors and `│  `/`   `
// continuation prefixes. Nodes deeper than maxDepth are folded into a
// single marker so a pathological session can't flood the terminal.
func printReplayTree(w io.Writer, r *ReplayResult, maxDepth int) {
	if r == nil || len(r.Root) == 0 {
		fmt.Fprintln(w, "no audit rows in this session")
		return
	}
	for i := range r.Root {
		printReplayNode(w, &r.Root[i], "", i == len(r.Root)-1, 0, maxDepth)
	}
}

// printReplayNode renders one node and recurses into its children.
// `prefix` is the accumulated indentation for this node's *children*'s
// continuation lines; `isLast` selects the connector for this node.
func printReplayNode(
	w io.Writer,
	node *ReplayNode,
	prefix string,
	isLast bool,
	depth, maxDepth int,
) {
	connector := "├── "
	childPrefix := prefix + "│   "
	if isLast {
		connector = "└── "
		childPrefix = prefix + "    "
	}
	fmt.Fprintf(w, "%s%s%s\n", prefix, connector, formatReplayNode(node))

	if depth >= maxDepth {
		if hidden := countDescendants(node); hidden > 0 {
			fmt.Fprintf(w, "%s└── … %d more node(s) below depth %d (raise --max-depth to expand)\n",
				childPrefix, hidden, maxDepth)
		}
		return
	}
	for i := range node.Children {
		printReplayNode(
			w, &node.Children[i], childPrefix, i == len(node.Children)-1, depth+1, maxDepth)
	}
}

// formatReplayNode renders one node's single line:
//
//	<occurred_at> <op_id> [<result_status>] (<duration_ms>ms)
//
// `occurred_at` is the node's `ts` field (the audit row's
// `occurred_at`). A null duration renders as `(-ms)` so the column
// stays present and grep-friendly across nodes.
func formatReplayNode(node *ReplayNode) string {
	dur := strDeref(node.DurationMS)
	if dur == "" {
		dur = "-"
	}
	return fmt.Sprintf("%s %s [%s] (%sms)", node.TS, node.OpID, node.ResultStatus, dur)
}

// countDescendants counts every node strictly below `node` (its whole
// subtree minus itself). Used to summarise how many nodes the
// --max-depth fold hid.
func countDescendants(node *ReplayNode) int {
	total := 0
	for i := range node.Children {
		total += 1 + countDescendants(&node.Children[i])
	}
	return total
}
