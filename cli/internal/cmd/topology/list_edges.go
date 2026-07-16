// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newListEdgesCmd returns the `meho topology list-edges` command.
//
//	meho topology list-edges [--kind <type>] [--source curated|auto]
//	  [--from <node>] [--to <node>] [--conflicts] [--limit N]
//	  [--offset N] [--json] [--backplane <url>]
//	# GET /api/v1/topology/edges?kind=&source=&from=&to=&conflicts=&limit=&offset=
//
// Tenant-scoped flat listing of curated + auto edges. The tenant
// boundary is server-side (the JWT) — no flag accepts a tenant id, and
// cross-tenant queries return the same empty list a missing node would.
func newListEdgesCmd() *cobra.Command {
	var (
		kindFilter        string
		sourceFilter      string
		fromFilter        string
		toFilter          string
		conflictsOnly     bool
		limit             int
		offset            int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list-edges",
		Short: "List curated + auto topology edges (filterable, tenant-scoped)",
		Long: "list-edges calls GET /api/v1/topology/edges and renders " +
			"a flat tenant-scoped edge listing. All filters are " +
			"optional and combine with AND: --kind narrows to one " +
			"edge kind, --source picks curated-only or auto-only, " +
			"--from / --to filter by endpoint name, --conflicts " +
			"surfaces only edges flagged by the §6 conflict detector " +
			"(usually an auto edge that an annotation supersedes — " +
			"the recoverability listing). Default output is an " +
			"aligned table sorted newest-first by last_seen; --json " +
			"emits the raw response array.",
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runListEdges(cmd, listEdgesOptions{
				Kind:              kindFilter,
				Source:            sourceFilter,
				From:              fromFilter,
				To:                toFilter,
				Conflicts:         conflictsOnly,
				Limit:             limit,
				Offset:            offset,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&kindFilter, "kind", "",
		"restrict to one edge kind (run `meho topology annotate --help` for the closed 10-kind vocabulary)")
	cmd.Flags().StringVar(&sourceFilter, "source", "",
		"restrict by source: `curated` (operator-asserted) or `auto` (probe-derived)")
	cmd.Flags().StringVar(&fromFilter, "from", "",
		"filter to edges whose `from` endpoint matches this node name")
	cmd.Flags().StringVar(&toFilter, "to", "",
		"filter to edges whose `to` endpoint matches this node name")
	cmd.Flags().BoolVar(&conflictsOnly, "conflicts", false,
		"surface only edges flagged by the §6 conflict detector (recoverability listing)")
	cmd.Flags().IntVar(&limit, "limit", 0,
		"max edges to return (1..1000, server default 200 when omitted)")
	cmd.Flags().IntVar(&offset, "offset", 0,
		"pagination offset (default 0)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type listEdgesOptions struct {
	Kind              string
	Source            string
	From              string
	To                string
	Conflicts         bool
	Limit             int
	Offset            int
	JSONOut           bool
	BackplaneOverride string
}

func runListEdges(cmd *cobra.Command, opts listEdgesOptions) error {
	if opts.Source != "" && opts.Source != "curated" && opts.Source != "auto" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--source must be `curated` or `auto`; got %q", opts.Source)),
			opts.JSONOut,
		)
	}
	if opts.Limit < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--limit must be >= 0; got %d", opts.Limit)),
			opts.JSONOut,
		)
	}
	if opts.Offset < 0 {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("--offset must be >= 0; got %d", opts.Offset)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	edges, statusCode, body, err := getEdges(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if edges == nil {
		// 200 OK with a missing payload — fail loud rather than
		// silently rendering "no edges matched". Mirrors the kb /
		// memory iter-2 nil-guard pattern.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a list-edges payload", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), edges)
	}
	printEdgeListing(cmd.OutOrStdout(), edges)
	return nil
}

// buildListEdgesParams maps the CLI flags onto the generated query-
// param shape. Every filter is omitted from the wire when unset so
// the server applies its defaults (no kind / source filter,
// limit=200, offset=0). The `--source` flag maps directly to the
// `?source=` query param (the `graph_edge.source` column literal —
// `auto` or `curated`); the route's regex pattern enforces the same
// closed pair.
//
// `Kind` rides as a plain `*string` (the wire schema is an open
// slug-patterned string since T1 #2534); we reuse the operator-typed
// string verbatim — malformed kinds round-trip to a 422 with the
// pattern message rather than failing client-side, so the CLI never
// second-guesses the substrate's vocabulary check.
func buildListEdgesParams(opts listEdgesOptions) *api.ListEdgesRouteApiV1TopologyEdgesGetParams {
	params := &api.ListEdgesRouteApiV1TopologyEdgesGetParams{}
	if opts.Kind != "" {
		k := opts.Kind
		params.Kind = &k
	}
	if opts.Source != "" {
		s := opts.Source
		params.Source = &s
	}
	if opts.From != "" {
		f := opts.From
		params.From = &f
	}
	if opts.To != "" {
		t := opts.To
		params.To = &t
	}
	if opts.Conflicts {
		c := true
		params.Conflicts = &c
	}
	if opts.Limit > 0 {
		l := opts.Limit
		params.Limit = &l
	}
	if opts.Offset > 0 {
		o := opts.Offset
		params.Offset = &o
	}
	return params
}

func getEdges(
	ctx context.Context,
	backplaneURL string,
	opts listEdgesOptions,
) ([]api.TopologyEdge, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	params := buildListEdgesParams(opts)
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ListEdgesRouteApiV1TopologyEdgesGetResponse, error) {
			return authed.ListEdgesRouteApiV1TopologyEdgesGetWithResponse(ctx, params)
		},
		func(r *api.ListEdgesRouteApiV1TopologyEdgesGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, 0, nil, err
	}
	if resp.JSON200 != nil {
		return *resp.JSON200, resp.StatusCode(), resp.Body, nil
	}
	return nil, resp.StatusCode(), resp.Body, nil
}

// printEdgeListing renders edges as an aligned table. Columns: KIND,
// SOURCE, FROM (kind/name), TO (kind/name), LAST_SEEN. The id column
// is omitted from the default view to keep rows scannable; --json
// emits the full envelope including ids for piping into `unannotate
// <edge-id>`. Empty listing → an explanatory line so an operator can
// tell "tenant has no curated edges yet" from "filter matched nothing".
//
// `e.LastSeen` is a `*time.Time` in the generated client; the table
// renders the RFC3339 form to preserve operator-readable timestamps
// for audit correlation.
func printEdgeListing(w io.Writer, edges []api.TopologyEdge) {
	if len(edges) == 0 {
		fmt.Fprintln(w, "no edges matched")
		return
	}
	fmt.Fprintf(w, "%-18s %-8s %-30s %-30s %s\n",
		"KIND", "SOURCE", "FROM", "TO", "LAST_SEEN")
	for _, e := range edges {
		lastSeen := "-"
		if e.LastSeen != nil && !e.LastSeen.IsZero() {
			lastSeen = e.LastSeen.UTC().Format("2006-01-02T15:04:05Z")
		}
		from := fmt.Sprintf("%s/%s", e.From.Kind, e.From.Name)
		to := fmt.Sprintf("%s/%s", e.To.Kind, e.To.Name)
		fmt.Fprintf(w, "%-18s %-8s %-30s %-30s %s\n",
			truncate(e.Kind, 18),
			truncate(e.Source, 8),
			truncate(from, 30),
			truncate(to, 30),
			lastSeen,
		)
	}
}
