// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newUnannotateCmd returns the `meho topology unannotate` command.
//
//	meho topology unannotate <edge-id>
//	meho topology unannotate <from> <kind> <to> [--from-kind K] [--to-kind K]
//	# DELETE /api/v1/topology/edges/<edge_id>
//
// The id form maps directly to the DELETE route. The tuple form is
// **client-side**: a GET /api/v1/topology/edges?from=&kind=&to=&source=curated
// resolves the unique curated edge and then a DELETE deletes by id.
// T5's DELETE accepts only `{edge_id}` in the path (no tuple-form
// DELETE route) so the resolution must happen here, not at the route.
//
// The auto-vs-curated rule (§3 of Initiative #364): a 409 from the
// route indicates the targeted edge has `source='auto'` — auto edges
// resurrect on the next refresh, so manual deletion is meaningless.
// The CLI surfaces the server's `detail.message` verbatim so an
// operator sees the annotate-over-auto remediation guidance.
func newUnannotateCmd() *cobra.Command {
	var (
		fromKind          string
		toKind            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "unannotate <edge-id> | <from> <kind> <to>",
		Short: "Delete a curated topology edge (by id or by from/kind/to tuple)",
		Long: "unannotate deletes a curated topology edge. Two forms:\n\n" +
			"  meho topology unannotate <edge-id>\n" +
			"  meho topology unannotate <from> <kind> <to>\n\n" +
			"The id form maps to DELETE /api/v1/topology/edges/<edge_id> " +
			"directly. The tuple form is resolved client-side: a GET " +
			"locates the unique curated edge matching the triple, then " +
			"a DELETE removes it by id. Requires tenant_admin.\n\n" +
			"Auto edges (source='auto') cannot be deleted — they " +
			"resurrect on the next refresh, so the server returns 409 " +
			"with the annotate-over-auto remediation hint. The CLI " +
			"surfaces that hint verbatim. A missing or cross-tenant " +
			"edge returns 404 (the tenant boundary is opaque — a row " +
			"in another tenant is indistinguishable from a missing row).",
		Args: func(_ *cobra.Command, args []string) error {
			if len(args) != 1 && len(args) != 3 {
				return errors.New(
					"unannotate requires either <edge-id> or <from> <kind> <to> " +
						"(got " + fmt.Sprintf("%d", len(args)) + " args)")
			}
			return nil
		},
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts := unannotateOptions{
				FromKindPin:       fromKind,
				ToKindPin:         toKind,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			}
			switch len(args) {
			case 1:
				opts.EdgeID = args[0]
			case 3:
				opts.From = args[0]
				opts.Kind = args[1]
				opts.To = args[2]
			}
			return runUnannotate(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&fromKind, "from-kind", "",
		"pin the `from` endpoint to one node kind when its name is ambiguous (tuple form only)")
	cmd.Flags().StringVar(&toKind, "to-kind", "",
		"pin the `to` endpoint to one node kind when its name is ambiguous (tuple form only)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON ({\"deleted\": \"<edge_id>\"}) instead of the human line")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type unannotateOptions struct {
	// EdgeID is set when the operator passed a single argument (the id
	// form). The CLI validates it as a UUID before issuing the DELETE.
	EdgeID string
	// From / Kind / To are set when the operator passed three arguments
	// (the tuple form). The CLI runs a client-side GET to resolve the
	// unique edge id before issuing the DELETE.
	From string
	Kind string
	To   string
	// FromKindPin / ToKindPin disambiguate an endpoint whose bare name
	// resolves to multiple node kinds in the tenant — the same lever
	// the list-edges / annotate / closure verbs expose.
	FromKindPin       string
	ToKindPin         string
	JSONOut           bool
	BackplaneOverride string
}

func runUnannotate(cmd *cobra.Command, opts unannotateOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}

	edgeIDStr := opts.EdgeID
	if edgeIDStr == "" {
		// Tuple form — resolve client-side. The list helper applies the
		// tenant scope server-side, so a cross-tenant tuple returns an
		// empty list (rendered as "not found"), never another tenant's
		// row. Pin source=curated because the auto-row deletion would
		// 409 anyway, and an auto edge accidentally selected by a
		// matching tuple would block the curated row that the operator
		// actually wants to remove.
		resolved, err := resolveCuratedEdgeID(cmd.Context(), backplaneURL, opts)
		if err != nil {
			return renderUnannotateResolveError(cmd, backplaneURL, err, opts)
		}
		edgeIDStr = resolved
	}
	edgeUUID, perr := uuid.Parse(edgeIDStr)
	if perr != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"invalid <edge-id> %q: not a UUID (%v)", edgeIDStr, perr)),
			opts.JSONOut)
	}

	if err := deleteEdge(cmd.Context(), backplaneURL, edgeUUID); err != nil {
		return renderUnannotateDeleteError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(),
			map[string]string{"deleted": edgeUUID.String()})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted edge %s\n", edgeUUID)
	return nil
}

// resolveCuratedEdgeID issues a GET /api/v1/topology/edges with the
// from/kind/to triple + source=curated and returns the single matching
// edge id. The route applies the tenant scope server-side, so a cross-
// tenant tuple resolves to an empty result. Multiple matches are
// possible when the operator didn't pass --from-kind / --to-kind on an
// ambiguous endpoint; the helper surfaces an explanatory error rather
// than silently picking one.
func resolveCuratedEdgeID(
	ctx context.Context,
	backplaneURL string,
	opts unannotateOptions,
) (string, error) {
	listOpts := listEdgesOptions{
		Kind:   opts.Kind,
		Source: "curated",
		From:   opts.From,
		To:     opts.To,
		Limit:  2, // 2 is enough to detect ambiguity without dragging back the world.
	}
	edges, statusCode, body, err := getEdges(ctx, backplaneURL, listOpts)
	if err != nil {
		return "", err
	}
	if statusCode != http.StatusOK {
		// Surface a synthetic httpStatusError so the caller can route
		// to the same renderHTTPStatus path the deleted-edge call uses.
		return "", &httpStatusError{StatusCode: statusCode, Body: body}
	}
	// Filter client-side by endpoint kind when the operator pinned one
	// — the list route filters by name only.
	if opts.FromKindPin != "" || opts.ToKindPin != "" {
		filtered := edges[:0]
		for _, e := range edges {
			if opts.FromKindPin != "" && e.From.Kind != opts.FromKindPin {
				continue
			}
			if opts.ToKindPin != "" && e.To.Kind != opts.ToKindPin {
				continue
			}
			filtered = append(filtered, e)
		}
		edges = filtered
	}
	switch len(edges) {
	case 0:
		return "", &unannotateResolveError{kind: "not_found"}
	case 1:
		return edges[0].Id.String(), nil
	default:
		ids := make([]string, 0, len(edges))
		for _, e := range edges {
			ids = append(ids, fmt.Sprintf(
				"%s (%s/%s --[%s]--> %s/%s)",
				e.Id, e.From.Kind, e.From.Name, e.Kind, e.To.Kind, e.To.Name))
		}
		return "", &unannotateResolveError{
			kind:    "ambiguous",
			matches: ids,
		}
	}
}

// unannotateResolveError signals a tuple-form resolution failure so
// the caller can surface a CLI-shaped diagnostic distinct from a
// transport / HTTP error. `kind` is "not_found" or "ambiguous";
// `matches` carries the rendered candidates for the ambiguous case.
type unannotateResolveError struct {
	kind    string
	matches []string
}

func (e *unannotateResolveError) Error() string {
	if e.kind == "ambiguous" {
		return "ambiguous tuple matches multiple edges"
	}
	return "no matching curated edge"
}

// httpStatusError is the bridge for non-2xx responses surfaced from a
// helper that has no direct cobra context (e.g. resolveCuratedEdgeID).
// Carries (statusCode, body) so the caller's renderUnannotate* helper
// can route through the shared renderHTTPStatus / renderUnannotateDeleteError
// ladder.
type httpStatusError struct {
	StatusCode int
	Body       []byte
}

func (e *httpStatusError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, strings.TrimSpace(string(e.Body)))
}

func renderUnannotateResolveError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	opts unannotateOptions,
) error {
	var rerr *unannotateResolveError
	if errors.As(err, &rerr) {
		if rerr.kind == "ambiguous" {
			msg := fmt.Sprintf(
				"tuple %q --[%s]--> %q matches %d curated edges; "+
					"re-run `meho topology unannotate <edge-id>` with one of:\n  - %s",
				opts.From, opts.Kind, opts.To,
				len(rerr.matches), strings.Join(rerr.matches, "\n  - "))
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(msg), opts.JSONOut)
		}
		// not_found
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"no curated edge matches %q --[%s]--> %q in this tenant",
				opts.From, opts.Kind, opts.To)),
			opts.JSONOut)
	}
	var he *httpStatusError
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.StatusCode, he.Body, opts.JSONOut)
	}
	return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
}

// renderUnannotateDeleteError intercepts the route's 409 auto-edge
// envelope so the operator sees the server's `detail.message`
// verbatim — the annotate-over-auto remediation guidance — instead of
// the raw 409 body the generic renderHTTPStatus would surface.
func renderUnannotateDeleteError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	var he *httpStatusError
	if errors.As(err, &he) && he.StatusCode == http.StatusConflict {
		if msg := formatAutoEdgeConflict(string(he.Body)); msg != "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(msg), jsonOut)
		}
		return renderHTTPStatus(cmd, backplaneURL, he.StatusCode, he.Body, jsonOut)
	}
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.StatusCode, he.Body, jsonOut)
	}
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// autoEdgeConflictDetail mirrors the 409 body the unannotate route
// emits for an auto-row delete attempt:
// `{"detail":{"error":"auto_edge_deletion","edge_id":"...","message":"..."}}`.
type autoEdgeConflictDetail struct {
	Error   string `json:"error"`
	EdgeID  string `json:"edge_id"`
	Message string `json:"message"`
}

// formatAutoEdgeConflict pulls the server's `detail.message` out of the
// 409 envelope and prefixes it with the edge id. Returns the empty
// string when the body doesn't match the auto_edge_deletion shape —
// the caller then falls back to the generic 409 renderer.
func formatAutoEdgeConflict(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err != nil {
		return ""
	}
	var detail autoEdgeConflictDetail
	if err := json.Unmarshal(env.Detail, &detail); err != nil {
		return ""
	}
	if detail.Error != "auto_edge_deletion" {
		return ""
	}
	if detail.Message == "" {
		return fmt.Sprintf("cannot delete auto edge %s", detail.EdgeID)
	}
	return fmt.Sprintf("cannot delete edge %s: %s", detail.EdgeID, detail.Message)
}

// deleteEdge issues the DELETE call against the generated typed
// client. The route returns 204 No Content on success (whether the
// row existed or not — idempotent contract) and 409 + structured
// detail for an auto-row delete attempt. The caller forwards the
// returned error to renderUnannotateDeleteError for category mapping.
func deleteEdge(ctx context.Context, backplaneURL string, edgeID uuid.UUID) error {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.UnannotateEdgeRouteApiV1TopologyEdgesEdgeIdDeleteResponse, error) {
			return authed.UnannotateEdgeRouteApiV1TopologyEdgesEdgeIdDeleteWithResponse(ctx, edgeID, nil)
		},
		func(r *api.UnannotateEdgeRouteApiV1TopologyEdgesEdgeIdDeleteResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return err
	}
	if resp.StatusCode() == http.StatusNoContent {
		return nil
	}
	return &httpStatusError{StatusCode: resp.StatusCode(), Body: resp.Body}
}
