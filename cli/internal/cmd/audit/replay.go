// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/google/uuid"
	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
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
// rows) and exits non-zero — see renderReplayRequestError.
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
	// The typed-client's `sessionId` path parameter is
	// `openapi_types.UUID`. Parse the operator string at the verb
	// edge so a non-UUID argument surfaces as a clean
	// output.Unexpected before any network round-trip.
	parsed, err := uuid.Parse(strings.TrimSpace(opts.SessionID))
	if err != nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"replay requires a valid UUID <session-id>; %q is not a UUID", opts.SessionID)),
			opts.JSONOut,
		)
	}
	sessionID := openapi_types.UUID(parsed)
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
	client, cerr := newAuthedClient(cmd.Context(), cmd, backplaneURL, opts.JSONOut)
	if cerr != nil {
		return cerr
	}
	rawBody, result, err := fetchReplay(cmd.Context(), client, sessionID)
	if err != nil {
		return renderReplayRequestError(cmd, backplaneURL, opts.SessionID, err, opts.JSONOut)
	}
	if opts.JSONOut {
		// Emit the server bytes verbatim so every audit column on each
		// node survives the round-trip — the compliance-export contract
		// must not be lossy through the CLI's typed-decode pass.
		_, werr := cmd.OutOrStdout().Write(append(rawBody, '\n'))
		return werr
	}
	if result == nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("backplane returned 200 OK but no JSON body decoded against AuditReplayResult"),
			opts.JSONOut,
		)
	}
	printReplayTree(cmd.OutOrStdout(), result, opts.MaxDepth)
	return nil
}

// fetchReplay drives the typed-client
// `ReplayApiV1AuditSessionsSessionIdReplayGet` endpoint with the
// same one-shot 401-retry shape `postQuery` uses. Returns the raw
// body bytes (for --json verbatim), the decoded result (for the
// tree render), and an `*httpResponseError` carrying the non-2xx
// status code + body for the renderer to classify.
func fetchReplay(
	ctx context.Context,
	client *api.AuthedClient,
	sessionID openapi_types.UUID,
) ([]byte, *api.AuditReplayResult, error) {
	resp, err := client.ReplayApiV1AuditSessionsSessionIdReplayGetWithResponse(ctx, sessionID, nil)
	if err != nil {
		return nil, nil, err
	}
	if resp.StatusCode() == 401 {
		if rerr := client.Refresh(ctx); rerr != nil {
			return nil, nil, rerr
		}
		resp, err = client.ReplayApiV1AuditSessionsSessionIdReplayGetWithResponse(ctx, sessionID, nil)
		if err != nil {
			return nil, nil, err
		}
	}
	if resp.StatusCode() < 200 || resp.StatusCode() >= 300 {
		return nil, nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.Body, resp.JSON200, nil
}

// renderReplayRequestError adds the 413 session-too-large redirect on
// top of the shared error ladder. A 413 carries the session's row count
// in its `{detail: {detail: "session_too_large", row_count: N}}` body;
// the verb turns it into an actionable `meho audit query --session-id`
// pointer (the query verb paginates the flat rows the over-cap tree
// can't render). Every other status falls through to the shared
// `routeRequestError` ladder used by the sibling verbs.
func renderReplayRequestError(
	cmd *cobra.Command,
	backplaneURL, sessionID string,
	err error,
	jsonOut bool,
) error {
	var he *httpResponseError
	if errors.As(err, &he) && he.statusCode == http.StatusRequestEntityTooLarge {
		rows := decodeSessionTooLargeRowCount(trimmedBody(he.body))
		msg := fmt.Sprintf(
			"session %s has %s rows (cap %d); use: meho audit query --session-id %s",
			sessionID, rows, replayRowCap, sessionID)
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(msg), jsonOut)
	}
	return routeRequestError(cmd, backplaneURL, err, jsonOut)
}

// printReplayTree renders the replay forest as an ASCII tree. Roots are
// emitted in chronological order (the server already orders both roots
// and children by (occurred_at, id)); each child is indented under its
// parent with the standard `├──`/`└──` connectors and `│  `/`   `
// continuation prefixes. Nodes deeper than maxDepth are folded into a
// single marker so a pathological session can't flood the terminal.
func printReplayTree(w io.Writer, r *api.AuditReplayResult, maxDepth int) {
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
	node *api.ReplayNode,
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
	if node.Children == nil {
		return
	}
	kids := *node.Children
	for i := range kids {
		printReplayNode(
			w, &kids[i], childPrefix, i == len(kids)-1, depth+1, maxDepth)
	}
}

// formatReplayNode renders one node's single line:
//
//	<occurred_at> <op_id> [<result_status>] (<duration_ms>ms)
//
// `occurred_at` is the node's `ts` field (the audit row's
// `occurred_at`). A null duration renders as `(-ms)` so the column
// stays present and grep-friendly across nodes.
func formatReplayNode(node *api.ReplayNode) string {
	dur := strDeref(node.DurationMs)
	if dur == "" {
		dur = "-"
	}
	return fmt.Sprintf("%s %s [%s] (%sms)", formatTS(node.Ts), node.OpId, node.ResultStatus, dur)
}

// countDescendants counts every node strictly below `node` (its whole
// subtree minus itself). Used to summarise how many nodes the
// --max-depth fold hid.
func countDescendants(node *api.ReplayNode) int {
	if node.Children == nil {
		return 0
	}
	kids := *node.Children
	total := 0
	for i := range kids {
		total += 1 + countDescendants(&kids[i])
	}
	return total
}
