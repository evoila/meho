// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// closureOptions is shared by the `dependents` and `dependencies`
// verbs — the two are mirror traversals over the identical REST
// query-param contract, so the option bag, request helper, and
// renderer are factored here and the per-verb files only differ in
// the `Verb` discriminator ("dependents" vs "dependencies") and the
// help prose.
type closureOptions struct {
	// Verb is the route discriminator: "dependents" or "dependencies".
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
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	nodes, statusCode, body, err := getClosure(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if nodes == nil {
		// 200 OK without a typed payload (e.g. content-type drift) —
		// fail loud rather than rendering "node not found" for what
		// is actually a contract mismatch. The kb / memory siblings
		// adopted the same JSON200-nil guard in their post-iter-2
		// fix loop.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a closure payload", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), nodes)
	}
	printNodeClosure(cmd.OutOrStdout(), opts.Name, nodes)
	return nil
}

// getClosure invokes the dependents-or-dependencies typed call against
// the supplied backplane URL and returns (parsed nodes, status code,
// raw body, transport-error). The (nodes, statusCode, body) triple is
// the same shape the kb / memory siblings settled on after their
// iter-2 fixes: callers branch on statusCode (200 → render `nodes`;
// non-200 → renderHTTPStatus with the raw body) without re-parsing
// the envelope.
func getClosure(
	ctx context.Context,
	backplaneURL string,
	opts closureOptions,
) ([]api.TopologyNode, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	// Build the typed param once; both branches feed it to the
	// matching `*WithResponse` method.
	var depthPtr *int
	if opts.Depth > 0 {
		d := opts.Depth
		depthPtr = &d
	}
	var kindFilterPtr *string
	if opts.EdgeKind != "" {
		k := opts.EdgeKind
		kindFilterPtr = &k
	}
	var anchorKindPtr *string
	if opts.NodeKind != "" {
		k := opts.NodeKind
		anchorKindPtr = &k
	}

	switch opts.Verb {
	case "dependents":
		params := &api.DependentsApiV1TopologyDependentsNameGetParams{
			Depth:      depthPtr,
			KindFilter: kindFilterPtr,
			Kind:       anchorKindPtr,
		}
		resp, err := retryOn401(ctx, authed,
			func(ctx context.Context) (*api.DependentsApiV1TopologyDependentsNameGetResponse, error) {
				return authed.DependentsApiV1TopologyDependentsNameGetWithResponse(ctx, opts.Name, params)
			},
			func(r *api.DependentsApiV1TopologyDependentsNameGetResponse) int { return r.StatusCode() },
		)
		if err != nil {
			return nil, 0, nil, err
		}
		if resp.JSON200 != nil {
			return *resp.JSON200, resp.StatusCode(), resp.Body, nil
		}
		return nil, resp.StatusCode(), resp.Body, nil
	case "dependencies":
		params := &api.DependenciesApiV1TopologyDependenciesNameGetParams{
			Depth:      depthPtr,
			KindFilter: kindFilterPtr,
			Kind:       anchorKindPtr,
		}
		resp, err := retryOn401(ctx, authed,
			func(ctx context.Context) (*api.DependenciesApiV1TopologyDependenciesNameGetResponse, error) {
				return authed.DependenciesApiV1TopologyDependenciesNameGetWithResponse(ctx, opts.Name, params)
			},
			func(r *api.DependenciesApiV1TopologyDependenciesNameGetResponse) int { return r.StatusCode() },
		)
		if err != nil {
			return nil, 0, nil, err
		}
		if resp.JSON200 != nil {
			return *resp.JSON200, resp.StatusCode(), resp.Body, nil
		}
		return nil, resp.StatusCode(), resp.Body, nil
	default:
		// Belt-and-braces: callers (newDependents/Dependencies) hard-
		// code the verb string, so a mismatch is a programmer error.
		return nil, 0, nil, fmt.Errorf("internal: unknown closure verb %q", opts.Verb)
	}
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
