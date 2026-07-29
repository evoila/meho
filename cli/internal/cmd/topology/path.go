// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// _maxHopsMax mirrors the API's Query(le=32) ceiling
// (backend/src/meho_backplane/api/v1/topology.py `_MAX_HOPS_MAX`).
const _maxHopsMax = 32

// newPathCmd returns the `meho topology path <from> <to>` command.
//
//	meho topology path <from> <to> [--max-hops N]
//	  [--from-kind K] [--to-kind K] [--include-stale=false] [--json] [--backplane <url>]
//	# GET /api/v1/topology/path?from=A&to=B&max_hops=N&include_stale=...
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
		includeStale      bool
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
				IncludeStale:      includeStale,
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
	cmd.Flags().BoolVar(&includeStale, "include-stale", true,
		"include soft-deleted (stale) nodes and edges in the search (last-refresh-wins); pass --include-stale=false for live rows only")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human chain")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type pathOptions struct {
	From     string
	To       string
	MaxHops  int
	FromKind string
	ToKind   string
	// IncludeStale mirrors the route's include_stale query param
	// (#2538); only sent on the wire when false (true is the server
	// default).
	IncludeStale      bool
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
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	path, statusCode, body, err := getPath(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	// 200 + nil result legitimately means "unreachable" (see getPath
	// docstring re: literal JSON `null` body). The JSON path emits
	// `null` so jq consumers see one stable contract; the table path
	// renders the no-path line via printPath's nil branch.
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), path)
	}
	printPath(cmd.OutOrStdout(), opts.From, opts.To, path)
	return nil
}

// buildPathParams maps the CLI flags onto the generated query-param
// shape. `From` / `To` are required (the path verb's two positional
// args); the optional kind pins and max_hops are omitted when unset
// so the server applies its default.
func buildPathParams(opts pathOptions) *api.PathApiV1TopologyPathGetParams {
	params := &api.PathApiV1TopologyPathGetParams{
		From: opts.From,
		To:   opts.To,
	}
	if opts.MaxHops > 0 {
		mh := opts.MaxHops
		params.MaxHops = &mh
	}
	if opts.FromKind != "" {
		fk := opts.FromKind
		params.FromKind = &fk
	}
	if opts.ToKind != "" {
		tk := opts.ToKind
		params.ToKind = &tk
	}
	if !opts.IncludeStale {
		f := false
		params.IncludeStale = &f
	}
	return params
}

// getPath invokes the path typed call. The route's response_model is
// `TopologyPath | None`: a literal JSON `null` body is the unreachable
// answer (the route returns 200 with body `null` rather than 404).
// oapi-codegen's generated parser populates `JSON200` to a zero-value
// `*TopologyPath` even for `null` bodies (`json.Unmarshal("null",
// &dest)` succeeds without touching dest); we explicitly detect the
// "null" body byte sequence and surface it as a nil result so the
// caller (printPath / the --json path) sees the unreachable shape
// unchanged from the pre-migration contract.
func getPath(
	ctx context.Context,
	backplaneURL string,
	opts pathOptions,
) (*api.TopologyPath, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	params := buildPathParams(opts)
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.PathApiV1TopologyPathGetResponse, error) {
			return authed.PathApiV1TopologyPathGetWithResponse(ctx, params)
		},
		func(r *api.PathApiV1TopologyPathGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, 0, nil, err
	}
	if resp.StatusCode() == http.StatusOK && isJSONNull(resp.Body) {
		return nil, resp.StatusCode(), resp.Body, nil
	}
	return resp.JSON200, resp.StatusCode(), resp.Body, nil
}

// isJSONNull reports whether the body is the literal JSON `null`
// token, ignoring leading/trailing whitespace. The path route uses
// this shape to signal unreachability; distinguishing it from a
// populated TopologyPath envelope cannot be done from the typed
// `*TopologyPath` alone because `json.Unmarshal("null", &dest)`
// leaves `dest` as the zero value, and the parser then stamps a
// non-nil `JSON200` pointing at it.
func isJSONNull(body []byte) bool {
	trimmed := strings.TrimSpace(string(body))
	return trimmed == "null"
}

// printPath renders the hop chain as `a -> b -> c (N hops)`, or the
// no-path line when the backend returned null (unreachable, missing
// endpoint, or cross-tenant — all the same answer).
func printPath(w io.Writer, from, to string, p *api.TopologyPath) {
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
