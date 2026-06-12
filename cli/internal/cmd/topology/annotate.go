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

// edgeKindVocabulary mirrors the closed 10-kind GraphEdgeKind enum
// (backend/src/meho_backplane/db/models.py). The order matches the
// enum declaration order so the in-help table is stable across
// releases — the four auto-discoverable kinds first, then the six
// curated-only kinds. The vocabulary is closed; widening it is a
// coordinated DB migration + enum + decision-row change per §12 of
// Initiative #364. Keep this list in lock-step with the enum.
var edgeKindVocabulary = []edgeKindEntry{
	// Four auto-discoverable kinds — refresh writes these on every probe.
	{"runs-on", "vm runs-on host, pod runs-on node (physical/scheduling host)"},
	{"mounts", "vm mounts datastore, pod mounts volume (storage attachment)"},
	{"routes-through", "ingress routes-through service, service routes-through pod (network)"},
	{"belongs-to", "pod belongs-to namespace, vm belongs-to host (logical group membership)"},
	// Six curated-only kinds — operator-asserted; cannot be derived from probes.
	{"authenticates-via", "principal -> identity-provider (e.g. k8s-sa-foo -> vault-role-bar)"},
	{"depends-on", "cross-system functional dependency (service-X -> database-Y)"},
	{"replicates-to", "operator-asserted replication between storage / database nodes"},
	{"backed-up-by", "operator-asserted backup relationship"},
	{"routes-via", "operator-asserted network path through an intermediary (vm-A -> firewall-X -> vm-B)"},
	{"policy-binds", "RBAC / policy attachment across connector boundaries (k8s-ns -> vault-policy)"},
}

type edgeKindEntry struct {
	Name string
	Desc string
}

// formatEdgeKindTable renders the 10-kind vocabulary as an aligned
// table block. Used both for the cobra `Long` help (§12 of Initiative
// #364 requires the table to be discoverable from `--help`) and for
// the unit tests that assert every name + description is present.
func formatEdgeKindTable() string {
	var b strings.Builder
	b.WriteString("Edge kind vocabulary (closed 10-kind enum, §12 of Initiative #364):\n\n")
	for _, e := range edgeKindVocabulary {
		fmt.Fprintf(&b, "  %-18s %s\n", e.Name, e.Desc)
	}
	return b.String()
}

// newAnnotateCmd returns the `meho topology annotate` command.
//
//	meho topology annotate <from> <kind> <to> [--note "..."]
//	  [--evidence-url URL] [--from-kind K] [--to-kind K]
//	  [--json] [--backplane <url>]
//	# POST /api/v1/topology/edges
//
// Creates or upserts a curated edge. The server runs §6 conflict
// detection on neighbouring auto edges so the resulting TopologyEdge
// `properties` may carry `conflicts_with` / `supersedes` markers; the
// CLI emits the raw response under --json so a consumer can react.
// Requires `tenant_admin` — a 403 from the backend is surfaced with
// the required-role hint.
func newAnnotateCmd() *cobra.Command {
	var (
		note              string
		evidenceURL       string
		fromKind          string
		toKind            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "annotate <from> <kind> <to>",
		Short: "Assert a curated topology edge (operator-curated cross-system relationship)",
		Long: "annotate calls POST /api/v1/topology/edges and asserts a " +
			"curated edge between two topology nodes — the operator " +
			"surface for the six cross-system relationships auto-" +
			"discovery cannot infer (authenticates-via, depends-on, " +
			"replicates-to, backed-up-by, routes-via, policy-binds), " +
			"and the recoverability lever for an incorrectly auto-" +
			"discovered edge (the §6 supersede flow). Idempotent — a " +
			"repeat call upserts the same row. Requires tenant_admin.\n\n" +
			"<kind> must be one of the closed 10-kind vocabulary " +
			"below. <from> and <to> are node names; pass --from-kind " +
			"/ --to-kind when a bare name is ambiguous across node " +
			"kinds (the backend 409s otherwise).\n\n" +
			formatEdgeKindTable(),
		Args:          cobra.ExactArgs(3),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runAnnotate(cmd, annotateOptions{
				From:              args[0],
				Kind:              args[1],
				To:                args[2],
				Note:              note,
				EvidenceURL:       evidenceURL,
				FromKind:          fromKind,
				ToKind:            toKind,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&note, "note", "",
		"free-form operator note (max 2000 chars) attached to the edge")
	cmd.Flags().StringVar(&evidenceURL, "evidence-url", "",
		"URL pointing at evidence for the asserted relationship (max 2000 chars)")
	cmd.Flags().StringVar(&fromKind, "from-kind", "",
		"pin the `from` endpoint to one node kind when its name is ambiguous")
	cmd.Flags().StringVar(&toKind, "to-kind", "",
		"pin the `to` endpoint to one node kind when its name is ambiguous")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw TopologyEdge response to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type annotateOptions struct {
	From              string
	Kind              string
	To                string
	Note              string
	EvidenceURL       string
	FromKind          string
	ToKind            string
	JSONOut           bool
	BackplaneOverride string
}

// buildAnnotateBody wraps the operator-typed annotate inputs into the
// generated `UnderscoreAnnotateEdgeRequest` body. `Kind` is forwarded
// as a plain string and validated server-side against the closed
// 10-kind GraphEdgeKind vocabulary (a 422 with the per-row message is
// the failure surface the operator sees if they typo the kind, same
// shape as the pre-migration contract). Endpoint kinds are passed as
// nil pointers when the operator didn't supply --from-kind / --to-kind
// so the wire shape sees an absent field rather than an empty string
// (the backend's _EdgeEndpoint model treats absent and empty kind
// differently — absent means "no pin", empty would fail validation).
func buildAnnotateBody(opts annotateOptions) api.UnderscoreAnnotateEdgeRequest {
	body := api.UnderscoreAnnotateEdgeRequest{
		From: api.UnderscoreEdgeEndpoint{Name: opts.From},
		Kind: api.GraphEdgeKind(opts.Kind),
		To:   api.UnderscoreEdgeEndpoint{Name: opts.To},
	}
	if opts.FromKind != "" {
		fk := opts.FromKind
		body.From.Kind = &fk
	}
	if opts.ToKind != "" {
		tk := opts.ToKind
		body.To.Kind = &tk
	}
	if opts.Note != "" {
		n := opts.Note
		body.Note = &n
	}
	if opts.EvidenceURL != "" {
		u := opts.EvidenceURL
		body.EvidenceUrl = &u
	}
	return body
}

func runAnnotate(cmd *cobra.Command, opts annotateOptions) error {
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	edge, statusCode, body, err := postAnnotate(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if statusCode != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if edge == nil {
		// Guard the 201-without-payload case so the renderer below
		// doesn't dereference nil. Mirrors the kb / memory iter-2
		// nil-guard pattern.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without an annotate payload", backplaneURL)),
			opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), edge)
	}
	printAnnotateSummary(cmd.OutOrStdout(), edge)
	return nil
}

func postAnnotate(
	ctx context.Context,
	backplaneURL string,
	opts annotateOptions,
) (*api.TopologyEdge, int, []byte, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, 0, nil, err
	}
	body := buildAnnotateBody(opts)
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.AnnotateEdgeRouteApiV1TopologyEdgesPostResponse, error) {
			return authed.AnnotateEdgeRouteApiV1TopologyEdgesPostWithResponse(ctx, nil, body)
		},
		func(r *api.AnnotateEdgeRouteApiV1TopologyEdgesPostResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, 0, nil, err
	}
	return resp.JSON201, resp.StatusCode(), resp.Body, nil
}

// printAnnotateSummary renders the resulting edge as a compact two-
// line block: the relationship in `kind/from -> kind/to` form (the
// same arrow notation the `path` verb uses) plus the edge id so the
// operator can pipe it into a follow-up `unannotate <id>`.
func printAnnotateSummary(w io.Writer, e *api.TopologyEdge) {
	fmt.Fprintf(w, "annotated edge: %s/%s --[%s]--> %s/%s\n",
		e.From.Kind, e.From.Name, e.Kind, e.To.Kind, e.To.Name)
	fmt.Fprintf(w, "  edge_id: %s\n", e.Id)
	fmt.Fprintf(w, "  source:  %s\n", e.Source)
}
