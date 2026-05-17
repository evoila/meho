// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// closureOptions is shared by the `dependents` and `dependencies`
// verbs — the two are mirror traversals over the identical REST
// query-param contract, so the option bag, path builder, request
// helper, and renderer are factored here and the per-verb files only
// differ in the route segment ("dependents" vs "dependencies") and
// the help prose.
type closureOptions struct {
	// Verb is the route segment: "dependents" or "dependencies".
	Verb string
	// Name is the anchor node name (or alias — alias→name resolution
	// is the backend's job; this is passed through verbatim).
	Name string
	// Depth caps the recursive walk. 0 means "omit; server default
	// (16)". The API clamps to [1, 64]; the CLI fails fast on a
	// client-side out-of-range value to save a 422 round-trip.
	Depth int
	// EdgeKind filters the walk to graph_edges of this kind
	// (maps to the route's `kind_filter` query param). Empty → all
	// edge kinds.
	EdgeKind string
	// NodeKind pins the anchor to one graph_node.kind when a bare
	// name resolves to several (maps to the route's `kind` query
	// param). Empty → unpinned; an ambiguous name then returns 409
	// ambiguous_node and the renderer points the operator back here.
	NodeKind string
	// JSONOut emits the raw []TopologyNode envelope.
	JSONOut bool
	// BackplaneOverride overrides the configured backplane URL.
	BackplaneOverride string
}

// _depthMax mirrors the API's Query(le=64) ceiling
// (backend/src/meho_backplane/api/v1/topology.py `_DEPTH_MAX`). The
// CLI rejects an over-budget --depth so the operator sees the
// constraint instead of a 422.
const _depthMax = 64

func runClosure(cmd *cobra.Command, opts closureOptions) error {
	if opts.Depth < 0 || opts.Depth > _depthMax {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"--depth must be between 1 and %d (or 0/omitted for the server default of 16); got %d",
				_depthMax, opts.Depth)),
			opts.JSONOut,
		)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	nodes, err := getClosure(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), nodes)
	}
	printNodeClosure(cmd.OutOrStdout(), opts.Name, nodes)
	return nil
}

// buildClosurePath assembles the GET path + query string for a
// dependents/dependencies call. The name is a path segment
// (pathEscape keeps a slash/space in an operator-typed name from
// corrupting the URL); depth / kind_filter / kind ride the query
// string and are omitted when unset so the server applies its
// defaults. Exposed for unit tests.
func buildClosurePath(opts closureOptions) string {
	q := url.Values{}
	if opts.Depth > 0 {
		q.Set("depth", strconv.Itoa(opts.Depth))
	}
	if opts.EdgeKind != "" {
		q.Set("kind_filter", opts.EdgeKind)
	}
	if opts.NodeKind != "" {
		q.Set("kind", opts.NodeKind)
	}
	path := "/api/v1/topology/" + opts.Verb + "/" + pathEscape(opts.Name)
	if encoded := q.Encode(); encoded != "" {
		path = path + "?" + encoded
	}
	return path
}

func getClosure(ctx context.Context, backplaneURL string, opts closureOptions) ([]TopologyNode, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", buildClosurePath(opts), nil)
	if err != nil {
		return nil, err
	}
	var out []TopologyNode
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode %s response: %w", opts.Verb, err)
	}
	return out, nil
}

// addClosureFlags wires the shared flag set onto a dependents/
// dependencies command. Kept here so the two verbs cannot drift.
func addClosureFlags(
	cmd *cobra.Command,
	depth *int,
	edgeKind, nodeKind, backplane *string,
	jsonOut *bool,
) {
	cmd.Flags().IntVar(depth, "depth", 0,
		fmt.Sprintf("max traversal depth (1..%d, server default 16 when omitted)", _depthMax))
	cmd.Flags().StringVar(edgeKind, "kind", "",
		"restrict the walk to edges of this kind (e.g. runs-on, mounts, routes-through, belongs-to)")
	cmd.Flags().StringVar(nodeKind, "node-kind", "",
		"pin the anchor to one node kind when the name is ambiguous across kinds")
	cmd.Flags().BoolVar(jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(backplane, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
}
