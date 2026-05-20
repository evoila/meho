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

// Edge mirrors the backend TopologyEdge Pydantic model
// (backend/src/meho_backplane/topology/schemas.py). The wire shape uses
// `from` / `to` keys (`serialization_alias` on the Python side); the
// Go struct names the fields `From` / `To` and uses the same JSON tag
// so the response decodes cleanly. `Properties` is a free-form bag the
// service writes (conflict markers, supersede UUIDs, operator notes);
// rendered only in --json mode to keep the table view scannable.
// Hand-written rather than aliased to a generated client type for the
// same generated-client-decoupling reason the other verb trees document.
type Edge struct {
	ID         string         `json:"id"`
	From       EdgeEndpoint   `json:"from"`
	To         EdgeEndpoint   `json:"to"`
	Kind       string         `json:"kind"`
	Source     string         `json:"source"`
	Properties map[string]any `json:"properties"`
	LastSeen   *string        `json:"last_seen"`
}

// EdgeEndpoint mirrors backend TopologyEdgeEndpoint. Carries the three
// fields a human-readable edge summary needs: the node id, the kind,
// and the name. The full node properties bag is not included — an
// edge listing is a survey of relationships, not a node dump.
type EdgeEndpoint struct {
	ID   string `json:"id"`
	Kind string `json:"kind"`
	Name string `json:"name"`
}

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
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	edges, err := getEdges(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), edges)
	}
	printEdgeListing(cmd.OutOrStdout(), edges)
	return nil
}

// buildListEdgesPath assembles the GET path + query string. Every
// filter is omitted from the wire when unset so the server applies
// its defaults (no kind / source filter, limit=200, offset=0). The
// `--source` flag maps directly to the `?source=` query param (the
// `graph_edge.source` column literal — `auto` or `curated`); the
// route's regex pattern enforces the same closed pair. Exposed for
// unit tests.
func buildListEdgesPath(opts listEdgesOptions) string {
	q := url.Values{}
	if opts.Kind != "" {
		q.Set("kind", opts.Kind)
	}
	if opts.Source != "" {
		q.Set("source", opts.Source)
	}
	if opts.From != "" {
		q.Set("from", opts.From)
	}
	if opts.To != "" {
		q.Set("to", opts.To)
	}
	if opts.Conflicts {
		q.Set("conflicts", "true")
	}
	if opts.Limit > 0 {
		q.Set("limit", strconv.Itoa(opts.Limit))
	}
	if opts.Offset > 0 {
		q.Set("offset", strconv.Itoa(opts.Offset))
	}
	path := "/api/v1/topology/edges"
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getEdges(ctx context.Context, backplaneURL string, opts listEdgesOptions) ([]Edge, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildListEdgesPath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out []Edge
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode list-edges response: %w", err)
	}
	return out, nil
}

// printEdgeListing renders edges as an aligned table. Columns: KIND,
// SOURCE, FROM (kind/name), TO (kind/name), LAST_SEEN. The id column
// is omitted from the default view to keep rows scannable; --json
// emits the full envelope including ids for piping into `unannotate
// <edge-id>`. Empty listing → an explanatory line so an operator can
// tell "tenant has no curated edges yet" from "filter matched nothing".
func printEdgeListing(w io.Writer, edges []Edge) {
	if len(edges) == 0 {
		fmt.Fprintln(w, "no edges matched")
		return
	}
	fmt.Fprintf(w, "%-18s %-8s %-30s %-30s %s\n",
		"KIND", "SOURCE", "FROM", "TO", "LAST_SEEN")
	for _, e := range edges {
		lastSeen := "-"
		if e.LastSeen != nil && *e.LastSeen != "" {
			lastSeen = *e.LastSeen
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
