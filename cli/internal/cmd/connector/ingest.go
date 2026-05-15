// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

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
// product / version / impl_id are the connector_id triple stored on
// the endpoint_descriptor row; specs is the list of (method, path)
// providers that merge under that one connector_id (vSphere is the
// canonical multi-spec case — vcenter.yaml + vi-json.yaml).
//
// DryRun=true makes the backplane parse + plan without writing to
// the DB; the response carries the same IngestionResult shape but
// the GroupingResult field is null (no LLM call on dry-run).
type IngestRequest struct {
	Product string       `json:"product"`
	Version string       `json:"version"`
	ImplID  string       `json:"impl_id"`
	Specs   []SpecSource `json:"specs"`
	DryRun  bool         `json:"dry_run"`
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
		dryRun            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "ingest",
		Short: "Ingest one or more vendor specs into a new connector (staged state)",
		Long: "ingest parses each --spec URI, registers the operations into the\n" +
			"endpoint_descriptor table under one connector_id, and runs the\n" +
			"LLM-summarised grouping pass. The newly-ingested connector lands\n" +
			"in review_status=staged — operations are NOT dispatchable until\n" +
			"an operator runs `meho connector review <id>` + `meho connector\n" +
			"enable <id>`.\n\n" +
			"--spec accepts three URI shapes:\n" +
			"  - file:///abs/path/to/spec.yaml          (local file)\n" +
			"  - https://example.com/spec.yaml           (HTTP fetch)\n" +
			"  - docs:<product-version>/<spec.yaml>      (resolves against\n" +
			"    $CLAUDE_RDC_DOCS when set; otherwise passed through for\n" +
			"    backplane-side resolution against its own checked-in docs)\n\n" +
			"Repeat --spec to merge multiple specs under one connector_id\n" +
			"(vSphere is the canonical case: vcenter.yaml + vi-json.yaml).\n\n" +
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
				DryRun:            dryRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&product, "product", "",
		"product name (e.g. vmware, kubernetes); required")
	cmd.Flags().StringVar(&versionFlag, "version", "",
		"product version (e.g. 9.0, 1.x); required")
	cmd.Flags().StringVar(&implID, "impl", "",
		"impl identifier (e.g. vmware-rest, k8s-go); required")
	cmd.Flags().StringArrayVar(&specs, "spec", nil,
		"spec URI; repeat for multi-spec merge under one connector_id (required, at least one)")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"parse and plan without writing to the DB; the response carries an IngestionResult with counts but no GroupingResult")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("product")
	_ = cmd.MarkFlagRequired("version")
	_ = cmd.MarkFlagRequired("impl")
	_ = cmd.MarkFlagRequired("spec")
	return cmd
}

type ingestOptions struct {
	Product           string
	Version           string
	ImplID            string
	Specs             []string
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

func runIngest(cmd *cobra.Command, opts ingestOptions) error {
	// Validate + resolve the spec URIs locally so the operator gets a
	// fast hint on a typo'd scheme rather than a backplane 422.
	if len(opts.Specs) == 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("at least one --spec is required"),
			opts.JSONOut,
		)
	}
	resolved := make([]SpecSource, 0, len(opts.Specs))
	for _, raw := range opts.Specs {
		uri, err := resolveSpecURI(raw)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(err.Error()),
				opts.JSONOut,
			)
		}
		resolved = append(resolved, SpecSource{URI: uri})
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	result, err := postIngest(cmd.Context(), backplaneURL, IngestRequest{
		Product: opts.Product,
		Version: opts.Version,
		ImplID:  opts.ImplID,
		Specs:   resolved,
		DryRun:  opts.DryRun,
	})
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printIngestSummary(cmd.OutOrStdout(), opts, result)
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
