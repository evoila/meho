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

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// TopologyPath mirrors the backend TopologyPath Pydantic model
// (backend/src/meho_backplane/topology/schemas.py). `Nodes` runs from
// the `from` node (depth 0) to the `to` node (depth == TotalHops)
// inclusive; `TotalHops == len(Nodes) - 1`. The route returns JSON
// `null` (HTTP 200) when `to` is unreachable from `from` within
// max_hops, or either endpoint does not exist in the tenant —
// unreachability is a valid answer, not an error.
type TopologyPath struct {
	Nodes     []TopologyNode `json:"nodes"`
	TotalHops int            `json:"total_hops"`
}

// _maxHopsMax mirrors the API's Query(le=32) ceiling
// (backend/src/meho_backplane/api/v1/topology.py `_MAX_HOPS_MAX`).
const _maxHopsMax = 32

// newPathCmd returns the `meho topology path <from> <to>` command.
//
//	meho topology path <from> <to> [--max-hops N]
//	  [--from-kind K] [--to-kind K] [--json] [--backplane <url>]
//	# GET /api/v1/topology/path?from=A&to=B&max_hops=N
//
// Shortest unweighted path between two named nodes. v0.2 is
// unweighted (every edge costs one hop) and walks edges in both
// directions, so the path follows connectivity rather than only edge
// orientation.
func newPathCmd() *cobra.Command {
	var (
		maxHops           int
		fromKind          string
		toKind            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "path <from> <to>",
		Short: "Find the shortest path between two nodes",
		Long: "path calls GET /api/v1/topology/path?from=A&to=B and " +
			"renders the shortest unweighted path (one of them, if " +
			"several tie) as an ordered hop chain, or the no-path line " +
			"when <to> is unreachable from <from> within --max-hops " +
			"(default 8, max 32) — unreachability and a missing " +
			"endpoint both render as \"no path\", never an error, and " +
			"a cross-tenant endpoint reads the same way. --from-kind / " +
			"--to-kind pin an endpoint when its bare name is ambiguous " +
			"across node kinds (the backend 409s otherwise).",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runPath(cmd, pathOptions{
				From:              args[0],
				To:                args[1],
				MaxHops:           maxHops,
				FromKind:          fromKind,
				ToKind:            toKind,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().IntVar(&maxHops, "max-hops", 0,
		fmt.Sprintf("max path length in hops (1..%d, server default 8 when omitted)", _maxHopsMax))
	cmd.Flags().StringVar(&fromKind, "from-kind", "",
		"pin the `from` endpoint to one node kind when its name is ambiguous")
	cmd.Flags().StringVar(&toKind, "to-kind", "",
		"pin the `to` endpoint to one node kind when its name is ambiguous")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human chain")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type pathOptions struct {
	From              string
	To                string
	MaxHops           int
	FromKind          string
	ToKind            string
	JSONOut           bool
	BackplaneOverride string
}

func runPath(cmd *cobra.Command, opts pathOptions) error {
	if opts.MaxHops < 0 || opts.MaxHops > _maxHopsMax {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--max-hops must be between 1 and %d (or 0/omitted for the server default of 8); got %d",
				_maxHopsMax, opts.MaxHops)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	path, err := getPath(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		// `path` is nil on the unreachable / missing-endpoint case;
		// PrintJSON emits literal `null`, the same shape the API
		// returns, so a jq consumer sees one stable contract.
		return output.PrintJSON(cmd.OutOrStdout(), path)
	}
	printPath(cmd.OutOrStdout(), opts.From, opts.To, path)
	return nil
}

// buildPathQuery assembles the GET /api/v1/topology/path query
// string. `from` / `to` match the route spec (`?from=A&to=B`); the
// optional kind pins and max_hops are omitted when unset so the
// server applies its default. Exposed for unit tests.
func buildPathQuery(opts pathOptions) string {
	q := url.Values{}
	q.Set("from", opts.From)
	q.Set("to", opts.To)
	if opts.MaxHops > 0 {
		q.Set("max_hops", strconv.Itoa(opts.MaxHops))
	}
	if opts.FromKind != "" {
		q.Set("from_kind", opts.FromKind)
	}
	if opts.ToKind != "" {
		q.Set("to_kind", opts.ToKind)
	}
	return "/api/v1/topology/path?" + q.Encode()
}

func getPath(ctx context.Context, backplaneURL string, opts pathOptions) (*TopologyPath, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildPathQuery(opts), nil)
	if err != nil {
		return nil, err
	}
	// The route's response_model is `TopologyPath | None`: a literal
	// JSON `null` is the unreachable answer, decoded here as a nil
	// *TopologyPath (distinct from a decode error).
	var out *TopologyPath
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode path response: %w", err)
	}
	return out, nil
}

// printPath renders the hop chain as `a -> b -> c (N hops)`, or the
// no-path line when the backend returned null (unreachable, missing
// endpoint, or cross-tenant — all the same answer).
func printPath(w io.Writer, from, to string, p *TopologyPath) {
	if p == nil || len(p.Nodes) == 0 {
		fmt.Fprintf(w, "no path from %q to %q within the hop budget\n", from, to)
		return
	}
	for i, n := range p.Nodes {
		if i > 0 {
			fmt.Fprint(w, " -> ")
		}
		fmt.Fprintf(w, "%s/%s", n.Kind, n.Name)
	}
	fmt.Fprintf(w, " (%d hop", p.TotalHops)
	if p.TotalHops != 1 {
		fmt.Fprint(w, "s")
	}
	fmt.Fprintln(w, ")")
}
