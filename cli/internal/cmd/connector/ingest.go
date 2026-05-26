// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// SpecSource mirrors the backend SpecSource Pydantic model (T6
// #406's IngestRequest.specs[] element). Single field for v0.2; the
// shape leaves room for per-spec overrides (e.g. spec_source tag,
// auth headers for HTTPS-fetched specs) in v0.2.next without a
// wire-shape break.
type SpecSource struct {
	URI string `json:"uri"`
}

// IngestRequest mirrors the backend IngestRequest Pydantic model.
// Two mutually-exclusive request shapes:
//
//   - Explicit-quadruple shape: product / version / impl_id / specs
//     carry the resolved triple plus the spec sources. The historical
//     manual-mode --product/--version/--impl/--spec form uses this.
//   - Catalog-driven shape (G0.14-T9 / #1150): catalog_entry carries a
//     "<product>/<version>" reference; the backplane resolves the
//     entry against the packaged catalog and fills in the quadruple
//     server-side. The --catalog flag uses this shape so REST-native
//     agent runtimes and the CLI share a single ingest path.
//
// Quadruple fields are pointers so the JSON serializer omits them on
// the catalog-driven shape: an empty string would fail the backend
// validator's mutual-exclusivity check (the empty quadruple is read
// as "explicit-quadruple supplied but blank").
//
// DryRun=true makes the backplane parse + plan without writing to
// the DB; the response carries the same IngestionResult shape but
// the GroupingResult field is null (no LLM call on dry-run).
type IngestRequest struct {
	Product      *string      `json:"product,omitempty"`
	Version      *string      `json:"version,omitempty"`
	ImplID       *string      `json:"impl_id,omitempty"`
	Specs        []SpecSource `json:"specs,omitempty"`
	CatalogEntry *string      `json:"catalog_entry,omitempty"`
	DryRun       bool         `json:"dry_run"`
}

// IngestionResult mirrors the canonical backend IngestionResultModel
// Pydantic model (operations/ingest/api_schemas.py) that lands with T6
// (#488). Counts cover the bulk-upsert outcome aggregated across every
// spec in the request; ConnectorID is the derived identifier the
// dispatcher uses for subsequent calls (`<impl_id>-<version>`, e.g.
// `vmware-rest-9.0`).
//
// connector_registered flips to true when this ingest call was the
// first to land the (product, version, impl_id) triple — the T2
// auto-registration of the GenericRestConnector shim ran. Subsequent
// ingests against the same triple return connector_registered=false.
//
// operations_grouped flips to true when the T3 LLM-grouping pass
// actually ran (every newly-ingested op got assigned to an
// OperationGroup row). False on the dry-run path and on the
// already-grouped-no-op path. The CLI renders it so the operator
// knows whether `meho connector review <id>` will have any groups
// to show.
type IngestionResult struct {
	ConnectorID         string `json:"connector_id"`
	InsertedCount       int    `json:"inserted_count"`
	UpdatedCount        int    `json:"updated_count"`
	SkippedCount        int    `json:"skipped_count"`
	ConnectorRegistered bool   `json:"connector_registered"`
	OperationsGrouped   bool   `json:"operations_grouped"`
}

// GroupingResult mirrors the canonical backend GroupingResultModel
// Pydantic model (operations/ingest/api_schemas.py). Counts cover the
// LLM-summarised grouping pass; the field is null on dry_run=true
// (no LLM call) and on the no-op re-run path (every op already
// grouped from a prior pass).
//
// LlmDurationMs is float (the Python field uses float seconds*1000)
// so the Go side mirrors it as float64 — an int truncation would
// silently drop sub-millisecond timings on fast LLM stub paths.
type GroupingResult struct {
	ConnectorID          string  `json:"connector_id"`
	GroupsCreated        int     `json:"groups_created"`
	OperationsAssigned   int     `json:"operations_assigned"`
	OperationsUnassigned int     `json:"operations_unassigned"`
	LLMCallCount         int     `json:"llm_call_count"`
	LLMDurationMs        float64 `json:"llm_duration_ms"`
}

// IngestResponse mirrors T6's IngestResponse Pydantic model. The
// Grouping field is null on a dry_run=true request because the LLM
// pass only runs when the operations actually land in the DB.
type IngestResponse struct {
	Ingestion IngestionResult `json:"ingestion"`
	Grouping  *GroupingResult `json:"grouping"`
}

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
func buildIngestRequest(opts ingestOptions) (IngestRequest, error) {
	if opts.Catalog != "" {
		catalog := opts.Catalog
		return IngestRequest{
			CatalogEntry: &catalog,
			DryRun:       opts.DryRun,
		}, nil
	}
	specs := make([]SpecSource, 0, len(opts.Specs))
	for _, raw := range opts.Specs {
		uri, uerr := resolveSpecURI(raw)
		if uerr != nil {
			return IngestRequest{}, uerr
		}
		specs = append(specs, SpecSource{URI: uri})
	}
	product := opts.Product
	version := opts.Version
	implID := opts.ImplID
	return IngestRequest{
		Product: &product,
		Version: &version,
		ImplID:  &implID,
		Specs:   specs,
		DryRun:  opts.DryRun,
	}, nil
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

func postIngest(ctx context.Context, backplaneURL string, body IngestRequest) (*IngestResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal ingest request: %w", err)
	}
	respBody, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/connectors/ingest", raw)
	if err != nil {
		return nil, err
	}
	var out IngestResponse
	if err := decodeJSON(respBody, "ingest", &out); err != nil {
		return nil, err
	}
	return &out, nil
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
func printIngestSummary(w io.Writer, opts ingestOptions, r *IngestResponse) {
	totalOps := r.Ingestion.InsertedCount + r.Ingestion.UpdatedCount + r.Ingestion.SkippedCount
	if opts.DryRun {
		fmt.Fprintf(w, "ingest %s/%s/%s — DRY RUN (no DB writes)\n",
			opts.Product, opts.Version, opts.ImplID,
		)
	} else {
		fmt.Fprintf(w, "ingest %s/%s/%s — connector_id=%s\n",
			opts.Product, opts.Version, opts.ImplID, r.Ingestion.ConnectorID,
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
		if r.Grouping.LLMCallCount > 0 {
			fmt.Fprintf(w, " (%d LLM call(s), %.0fms)",
				r.Grouping.LLMCallCount, r.Grouping.LLMDurationMs,
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
			r.Ingestion.ConnectorID, r.Ingestion.ConnectorID,
		)
	}
}
