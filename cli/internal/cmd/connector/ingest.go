// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// newIngestCmd returns the `meho connector ingest` command.
//
// CLI shape:
//
//	meho connector ingest \
//	  --product <p> --version <v> --impl <i> \
//	  --spec <uri> [--spec <uri> ...] \
//	  [--dry-run] [--json] [--backplane <url>]
//
// The verb hits POST /api/v1/connectors/ingest with an IngestRequest
// body; the backplane runs T1 parser → T2 register_ingested → T3
// LLM grouping and returns an IngestResponse envelope. tenant_admin
// role required (HTTP 403 → exit 5).
func newIngestCmd() *cobra.Command {
	var (
		product           string
		versionFlag       string
		implID            string
		specs             []string
		catalog           string
		dryRun            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "ingest",
		Short: "Ingest one or more vendor specs into a new connector (staged state)",
		Long: "ingest parses each spec, registers the operations into the\n" +
			"endpoint_descriptor table under one connector_id, and runs the\n" +
			"LLM-summarised grouping pass. The newly-ingested connector lands\n" +
			"in review_status=staged — operations are NOT dispatchable until\n" +
			"an operator runs `meho connector review <id>` + `meho connector\n" +
			"enable <id>`.\n\n" +
			"Two mutually-exclusive modes:\n\n" +
			"  Catalog mode: --catalog <product>/<version> resolves the curated\n" +
			"  catalog entry (see `meho connector catalog list`) and ingests its\n" +
			"  recommended triple + upstream spec URL(s). Typed-connector and\n" +
			"  fqdn-templated entries are refused with a hint.\n\n" +
			"  Manual mode: --product + --version + --impl + one-or-more --spec.\n" +
			"  --spec accepts three URI shapes:\n" +
			"    - file:///abs/path/to/spec.yaml          (local file)\n" +
			"    - https://example.com/spec.yaml           (HTTP fetch)\n" +
			"    - docs:<product-version>/<spec.yaml>      (resolves against\n" +
			"      $CLAUDE_RDC_DOCS when set; otherwise passed through for\n" +
			"      backplane-side resolution against its own checked-in docs)\n" +
			"  Repeat --spec to merge multiple specs under one connector_id\n" +
			"  (vSphere is the canonical case: vcenter.yaml + vi-json.yaml).\n\n" +
			"--dry-run parses + plans without writing to the DB; useful for\n" +
			"validating a spec before committing. Role: tenant_admin.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runIngest(cmd, ingestOptions{
				Product:           product,
				Version:           versionFlag,
				ImplID:            implID,
				Specs:             specs,
				Catalog:           catalog,
				DryRun:            dryRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&product, "product", "",
		"product name (e.g. vmware, kubernetes); manual mode (required with --version/--impl/--spec)")
	cmd.Flags().StringVar(&versionFlag, "version", "",
		"product version (e.g. 9.0, 1.x); manual mode")
	cmd.Flags().StringVar(&implID, "impl", "",
		"impl identifier (e.g. vmware-rest, k8s-go); manual mode")
	cmd.Flags().StringArrayVar(&specs, "spec", nil,
		"spec URI; repeat for multi-spec merge under one connector_id; manual mode")
	cmd.Flags().StringVar(&catalog, "catalog", "",
		"catalog mode: ingest the curated entry for <product>/<version> (e.g. vmware/9.0); "+
			"mutually exclusive with --product/--version/--impl/--spec")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"parse and plan without writing to the DB; the response carries an IngestionResult with counts but no GroupingResult")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type ingestOptions struct {
	Product           string
	Version           string
	ImplID            string
	Specs             []string
	Catalog           string
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

func runIngest(cmd *cobra.Command, opts ingestOptions) error {
	if err := validateIngestMode(opts); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}

	body, err := buildIngestRequest(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}

	result, err := postIngest(cmd.Context(), backplaneURL, body)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printIngestSummary(cmd.OutOrStdout(), opts, result)
	return nil
}

// buildIngestRequest assembles the POST body for either of the two
// mutually-exclusive request shapes (catalog-driven or explicit
// quadruple). Catalog mode (G0.14-T9 / #1150) ships the
// "<product>/<version>" reference verbatim — the backplane resolves
// the entry against the packaged catalog so REST-native clients and
// the CLI share the resolution path. Manual mode resolves each spec
// URI locally to give the operator a fast hint on a typo'd scheme.
//
// The generated `api.IngestRequest` carries the
// catalog-vs-quadruple-vs-`dry_run` discriminator on the wire via
// pointer fields the JSON serialiser omits when nil. Setting only
// what the operator asked for keeps the wire shape narrow and lets
// the backend's mutual-exclusivity validator stay green in both
// modes; the existing tests pin both branches.
func buildIngestRequest(opts ingestOptions) (api.IngestRequest, error) {
	body := api.IngestRequest{}
	if opts.DryRun {
		dr := true
		body.DryRun = &dr
	}
	if opts.Catalog != "" {
		catalog := opts.Catalog
		body.CatalogEntry = &catalog
		return body, nil
	}
	specs := make([]api.SpecSource, 0, len(opts.Specs))
	for _, raw := range opts.Specs {
		uri, uerr := resolveSpecURI(raw)
		if uerr != nil {
			return api.IngestRequest{}, uerr
		}
		specs = append(specs, api.SpecSource{Uri: uri})
	}
	product := opts.Product
	version := opts.Version
	implID := opts.ImplID
	body.Product = &product
	body.Version = &version
	body.ImplId = &implID
	body.Specs = &specs
	return body, nil
}

// validateIngestMode enforces the catalog/manual split: exactly one
// mode, and manual mode needs the full triple + at least one --spec.
// Replaces the per-flag MarkFlagRequired wiring (which can't express
// "required unless --catalog").
func validateIngestMode(opts ingestOptions) error {
	manualSet := opts.Product != "" || opts.Version != "" || opts.ImplID != "" || len(opts.Specs) > 0
	if opts.Catalog != "" {
		if manualSet {
			return errors.New(
				"--catalog cannot be combined with --product/--version/--impl/--spec; " +
					"use catalog mode OR manual mode, not both")
		}
		return nil
	}
	if !manualSet {
		return errors.New(
			"specify a connector to ingest: --catalog <product>/<version>, " +
				"or manual mode (--product --version --impl --spec)")
	}
	var missing []string
	if opts.Product == "" {
		missing = append(missing, "--product")
	}
	if opts.Version == "" {
		missing = append(missing, "--version")
	}
	if opts.ImplID == "" {
		missing = append(missing, "--impl")
	}
	if len(opts.Specs) == 0 {
		missing = append(missing, "--spec")
	}
	if len(missing) > 0 {
		return fmt.Errorf("manual ingest requires %s (or use --catalog <product>/<version>)",
			strings.Join(missing, ", "))
	}
	return nil
}

// postIngest drives the typed-client ingest endpoint with a one-shot
// 401-retry. The route declares `response_model=IngestResponse` so
// JSON200 carries the typed envelope; non-2xx surfaces as
// *httpResponseError for the caller to route through renderHTTPStatus.
func postIngest(ctx context.Context, backplaneURL string, body api.IngestRequest) (*api.IngestResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.IngestEndpointApiV1ConnectorsIngestPostResponse, error) {
			return authed.IngestEndpointApiV1ConnectorsIngestPostWithResponse(
				ctx,
				&api.IngestEndpointApiV1ConnectorsIngestPostParams{},
				body,
			)
		},
		func(r *api.IngestEndpointApiV1ConnectorsIngestPostResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		return nil, fmt.Errorf("backplane returned 200 OK but no JSON body decoded against IngestResponse")
	}
	return resp.JSON200, nil
}

// printIngestSummary renders an IngestResponse for human eyes. The
// shape matches what an operator running `meho connector ingest`
// interactively expects to read: connector_id first (so they can
// copy it into the subsequent `review` / `enable` commands), then
// the bulk-upsert counts, then the LLM grouping outcome (or "dry
// run — skipped" on dry-run).
//
// The canonical IngestionResultModel ships only the aggregate
// inserted/updated/skipped counts plus the two boolean flags
// (connector_registered, operations_grouped). The per-spec
// breakdown and the embeddings split that the original PR-body
// contract carried are not in the wire shape — operators see the
// aggregate via this rollup and the per-spec story via the audit log.
//
// The `<product>/<version>/<impl_id>` heading is derived from the
// response's `connector_id` rather than `opts.Product/Version/ImplID`
// because catalog mode (G0.14-T9 / #1150) leaves those opts empty —
// the backplane resolves the catalog entry server-side and returns
// the resolved triple via `connector_id`. Deriving from the response
// keeps the heading correct in both modes and matches the pre-#1150
// operator-visible output.
func printIngestSummary(w io.Writer, opts ingestOptions, r *api.IngestResponse) {
	totalOps := r.Ingestion.InsertedCount + r.Ingestion.UpdatedCount + r.Ingestion.SkippedCount
	heading := ingestSummaryHeading(r.Ingestion.ConnectorId)
	if opts.DryRun {
		fmt.Fprintf(w, "ingest %s — DRY RUN (no DB writes)\n", heading)
	} else {
		fmt.Fprintf(w, "ingest %s — connector_id=%s\n",
			heading, r.Ingestion.ConnectorId,
		)
	}
	fmt.Fprintf(w, "  operations: %d total (%d inserted / %d updated / %d skipped)\n",
		totalOps,
		r.Ingestion.InsertedCount,
		r.Ingestion.UpdatedCount,
		r.Ingestion.SkippedCount,
	)
	if !opts.DryRun {
		fmt.Fprintf(w, "  connector_registered: %t (first ingest of this triple flips it to true)\n",
			r.Ingestion.ConnectorRegistered,
		)
		fmt.Fprintf(w, "  operations_grouped: %t\n", r.Ingestion.OperationsGrouped)
	}
	if r.Grouping != nil {
		fmt.Fprintf(w, "  grouping: %d groups, %d ops assigned, %d unassigned",
			r.Grouping.GroupsCreated,
			r.Grouping.OperationsAssigned,
			r.Grouping.OperationsUnassigned,
		)
		if r.Grouping.LlmCallCount > 0 {
			fmt.Fprintf(w, " (%d LLM call(s), %.0fms)",
				r.Grouping.LlmCallCount, r.Grouping.LlmDurationMs,
			)
		}
		fmt.Fprintln(w)
	} else if !opts.DryRun {
		fmt.Fprintln(w, "  grouping: skipped (backplane returned no grouping result)")
	}
	if !opts.DryRun {
		fmt.Fprintf(w,
			"\nConnector is in review_status=staged. Next:\n"+
				"  meho connector review %s\n"+
				"  meho connector enable %s --confirm\n",
			r.Ingestion.ConnectorId, r.Ingestion.ConnectorId,
		)
	}
}

// ingestSummaryHeading derives the `<product>/<version>/<impl_id>`
// heading from a response connector_id. Both ingest modes route
// through this helper so the operator-visible output is identical
// to v0.6.0 regardless of which request shape (catalog or explicit
// quadruple) the CLI used. The backend resolves the catalog entry
// server-side, so deriving from the response is what makes the
// catalog-mode heading carry the resolved triple instead of empty
// `//` placeholders.
//
// Mirrors `parse_connector_id` in
// `backend/src/meho_backplane/operations/ingest/parser.py` — the
// operator-facing identifier is `<impl_id>-<version>` where
// `version` starts with a digit; `product` is the first
// dash-segment of `impl_id`. If the response carries a
// non-conforming connector_id (shouldn't happen in practice — the
// backend builds it from a validated triple) we fall back to
// echoing the connector_id verbatim so the operator still sees
// something useful instead of bare slashes.
func ingestSummaryHeading(connectorID string) string {
	for i, ch := range connectorID {
		if ch != '-' || i+1 >= len(connectorID) {
			continue
		}
		next := connectorID[i+1]
		if next < '0' || next > '9' {
			continue
		}
		implID := connectorID[:i]
		version := connectorID[i+1:]
		if implID == "" {
			return connectorID
		}
		product := implID
		if first := strings.IndexByte(implID, '-'); first != -1 {
			product = implID[:first]
		}
		return fmt.Sprintf("%s/%s/%s", product, version, implID)
	}
	return connectorID
}
