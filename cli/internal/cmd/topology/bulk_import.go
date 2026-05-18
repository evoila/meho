// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"

	"github.com/evoila/meho/cli/internal/output"
)

// bulkImportDoc is the on-disk root: a single `edges:` list. Decode
// into a typed struct rather than a generic map because every supported
// field maps 1:1 to the wire shape — there is no extras-spill contract
// for bulk-import the way targets/import.go has. yaml.v3's strict mode
// (KnownFields) would reject typo'd field names; we leave that off so
// the server-side validation surfaces every problem in one round trip
// rather than the CLI failing fast on the first typo.
type bulkImportDoc struct {
	Edges []bulkImportEdgeYAML `yaml:"edges" json:"edges"`
}

// bulkImportEdgeYAML mirrors the issue body's file shape:
// `{from, kind, to, note?, evidence_url?}`. `from` and `to` accept
// either a bare string (the common case — a name with no kind pin) or
// a nested `{name, kind}` map (when an endpoint is ambiguous and the
// operator wants to pin the kind). YAML nodes are decoded into
// `bulkImportEndpoint` via the custom UnmarshalYAML.
type bulkImportEdgeYAML struct {
	From        bulkImportEndpoint `yaml:"from" json:"from"`
	Kind        string             `yaml:"kind" json:"kind"`
	To          bulkImportEndpoint `yaml:"to" json:"to"`
	Note        string             `yaml:"note,omitempty" json:"note,omitempty"`
	EvidenceURL string             `yaml:"evidence_url,omitempty" json:"evidence_url,omitempty"`
}

// bulkImportEndpoint is the wire shape for one annotate endpoint.
// `Name` is required; `Kind` is the optional disambiguator for an
// ambiguous bare name. The YAML can spell each endpoint two ways:
//
//	from: svc-orders                 # bare string -> Name="svc-orders"
//	from: { name: svc-orders, kind: service }
type bulkImportEndpoint struct {
	Name string `json:"name" yaml:"name"`
	Kind string `json:"kind,omitempty" yaml:"kind,omitempty"`
}

// UnmarshalYAML accepts either a scalar (bare name) or a mapping
// node so the file format stays terse for the common case and explicit
// for the ambiguous case.
func (e *bulkImportEndpoint) UnmarshalYAML(node *yaml.Node) error {
	switch node.Kind {
	case yaml.ScalarNode:
		e.Name = node.Value
		return nil
	case yaml.MappingNode:
		// Decode into an alias struct to avoid infinite recursion via
		// this UnmarshalYAML method.
		type endpointMap struct {
			Name string `yaml:"name"`
			Kind string `yaml:"kind"`
		}
		var m endpointMap
		if err := node.Decode(&m); err != nil {
			return err
		}
		if m.Name == "" {
			return fmt.Errorf("endpoint missing required `name` field")
		}
		e.Name = m.Name
		e.Kind = m.Kind
		return nil
	default:
		return fmt.Errorf("endpoint must be a string or a {name, kind} map")
	}
}

// bulkImportRequest is the wire shape for
// POST /api/v1/topology/edges/bulk: `{edges: [...], dry_run: bool}`.
// The route accepts both “dry_run=true/false“ in the body and a
// canonical 200 response.
type bulkImportRequest struct {
	Edges  []bulkImportEdgeYAML `json:"edges"`
	DryRun bool                 `json:"dry_run,omitempty"`
}

// bulkImportResponseRow mirrors the per-row outcome from the service.
type bulkImportResponseRow struct {
	Index      int      `json:"index"`
	Action     string   `json:"action"`
	EdgeID     *string  `json:"edge_id"`
	FromName   string   `json:"from_name"`
	FromKind   string   `json:"from_kind"`
	ToName     string   `json:"to_name"`
	ToKind     string   `json:"to_kind"`
	Kind       string   `json:"kind"`
	Superseded []string `json:"superseded"`
	Conflicts  []string `json:"conflicts"`
}

type bulkImportResponse struct {
	DryRun    bool                    `json:"dry_run"`
	Created   int                     `json:"created"`
	Updated   int                     `json:"updated"`
	Conflicts int                     `json:"conflicts"`
	Rows      []bulkImportResponseRow `json:"rows"`
}

// bulkImportError mirrors the 422 invalid_bulk envelope.
type bulkImportErrorEnvelope struct {
	Error  string                 `json:"error"`
	Errors []bulkImportRowErrJSON `json:"errors"`
}

type bulkImportRowErrJSON struct {
	Index   int      `json:"index"`
	Error   string   `json:"error"`
	Message string   `json:"message"`
	Name    *string  `json:"name"`
	Kind    *string  `json:"kind"`
	Kinds   []string `json:"kinds"`
}

// newBulkImportCmd returns the `meho topology bulk-import` command.
//
//	meho topology bulk-import <file>
//	  [--dry-run]  # plan-only; no writes; no audit / broadcast events
//	  [--json]     # emit the raw response JSON instead of the human table
//	  [--backplane <url>]  # override the recorded backplane URL
//	# POST /api/v1/topology/edges/bulk
//
// Reads a YAML or JSON file matching `{edges: [{from, kind, to,
// note?, evidence_url?}]}` and posts the whole batch to the backplane
// in one transaction. The server runs validation (kind enum, endpoint
// resolution, §6 conflict detection) before any write; a single
// failing row aborts the entire batch (no partial apply). Requires
// `tenant_admin` — a 403 from the backplane is surfaced with the
// required-role hint.
//
// Dry-run shape. `--dry-run` returns the per-row plan (create /
// update / conflict classification) without writing any row, audit
// entry, or broadcast event. The plan is the operator's "what would
// land" preview before committing to the file.
func newBulkImportCmd() *cobra.Command {
	var (
		dryRun            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "bulk-import <file>",
		Short: "Annotate a list of curated topology edges from one file in a single transaction",
		Long: "bulk-import reads a YAML or JSON file shaped as " +
			"`edges: [{from, kind, to, note?, evidence_url?}]` and posts " +
			"the whole batch to POST /api/v1/topology/edges/bulk. The " +
			"server runs validation (kind enum, endpoint resolution, §6 " +
			"conflict detection) before any write; a single failing row " +
			"rejects the entire batch (no partial apply). Re-running the " +
			"same file is a per-row no-op — the idempotent annotate " +
			"upsert path matches the single-edge contract. Requires " +
			"tenant_admin.\n\n" +
			"`from` and `to` accept either a bare string (the common " +
			"case) or a `{name, kind}` map when an endpoint is " +
			"ambiguous and the operator wants to pin the kind. `kind` " +
			"must be one of the closed 10-kind GraphEdgeKind vocabulary " +
			"(`meho topology annotate --help` lists every kind).\n\n" +
			"--dry-run returns the per-row plan (create / update / " +
			"conflict) and writes nothing; --json emits the raw response " +
			"JSON for piping into a downstream consumer.\n\n" +
			formatEdgeKindTable(),
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runBulkImport(cmd, bulkImportOptions{
				File:              args[0],
				DryRun:            dryRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"compute the plan without applying any annotation (no writes, no audit, no broadcast events)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the raw POST /edges/bulk response JSON instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to import into (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type bulkImportOptions struct {
	File              string
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

func runBulkImport(cmd *cobra.Command, opts bulkImportOptions) error {
	data, err := os.ReadFile(opts.File)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("read %s: %v", opts.File, err)),
			opts.JSONOut)
	}
	doc, err := parseBulkImportDoc(data)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(err.Error()), opts.JSONOut)
	}
	if len(doc.Edges) == 0 {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("file contains no edges (the `edges:` list is missing or empty)"),
			opts.JSONOut)
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			classifyBackplaneError(err), opts.JSONOut)
	}
	body, err := json.Marshal(bulkImportRequest{
		Edges:  doc.Edges,
		DryRun: opts.DryRun,
	})
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("encode bulk-import body: %v", err)),
			opts.JSONOut)
	}
	resp, err := postBulkImport(cmd.Context(), backplaneURL, body)
	if err != nil {
		return renderBulkImportError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	printBulkImportSummary(cmd.OutOrStdout(), resp)
	return nil
}

// parseBulkImportDoc accepts YAML *or* JSON. yaml.v3's parser is a
// JSON superset for the input shape we accept (mappings + scalars), so
// the single yaml.Unmarshal call handles both — operators with a JSON
// file don't need a separate code path. Returns the validated document
// or a parse error.
func parseBulkImportDoc(data []byte) (*bulkImportDoc, error) {
	var doc bulkImportDoc
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("parse bulk-import file: %w", err)
	}
	// Source-order validation: surface missing required fields with
	// the row index so the operator can pinpoint the broken edge in
	// the source file without scanning every row.
	for i, e := range doc.Edges {
		if e.From.Name == "" {
			return nil, fmt.Errorf("entry %d: missing or empty `from` endpoint name", i)
		}
		if e.To.Name == "" {
			return nil, fmt.Errorf("entry %d: missing or empty `to` endpoint name", i)
		}
		if e.Kind == "" {
			return nil, fmt.Errorf("entry %d: missing or empty `kind` field", i)
		}
	}
	return &doc, nil
}

func postBulkImport(ctx context.Context, backplaneURL string, body []byte) (*bulkImportResponse, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "POST",
		"/api/v1/topology/edges/bulk", body)
	if err != nil {
		return nil, err
	}
	var out bulkImportResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("decode bulk-import response: %w", err)
	}
	return &out, nil
}

// renderBulkImportError intercepts the 422 `invalid_bulk` envelope so
// the per-row error list is rendered as a structured operator-readable
// block — one line per bad row pointing at the file index. Other
// errors fall back to the generic renderer.
func renderBulkImportError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	var he *httpError
	if errors.As(err, &he) && he.StatusCode == 422 {
		if msg := formatInvalidBulkEnvelope(he.Body); msg != "" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(msg), jsonOut)
		}
	}
	return renderRequestError(cmd, backplaneURL, err, jsonOut)
}

// formatInvalidBulkEnvelope renders the 422 `invalid_bulk` body into
// one operator-readable block. Returns the empty string when the body
// doesn't match the envelope so the caller falls back to the generic
// 422 renderer (Pydantic's standard validation shape).
func formatInvalidBulkEnvelope(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err != nil {
		return ""
	}
	var inner bulkImportErrorEnvelope
	if err := json.Unmarshal(env.Detail, &inner); err != nil {
		return ""
	}
	if inner.Error != "invalid_bulk" {
		return ""
	}
	var b strings.Builder
	fmt.Fprintf(&b, "bulk-import rejected: %d row(s) failed validation\n",
		len(inner.Errors))
	for _, e := range inner.Errors {
		fmt.Fprintf(&b, "  row %d: %s — %s\n", e.Index, e.Error, e.Message)
	}
	return b.String()
}

// printBulkImportSummary renders the apply / dry-run response. The
// summary header names the action counts; the per-row block surfaces
// the §6 conflict classifications + the edge ids so the operator can
// pipe a follow-up unannotate by id.
func printBulkImportSummary(w io.Writer, resp *bulkImportResponse) {
	mode := "applied"
	if resp.DryRun {
		mode = "planned (dry-run)"
	}
	fmt.Fprintf(w, "Bulk-import %s: %d to create, %d to update, %d with conflicts (%d rows total)\n",
		mode, resp.Created, resp.Updated, resp.Conflicts, len(resp.Rows))
	if len(resp.Rows) == 0 {
		return
	}
	fmt.Fprintf(w, "\n%-3s %-9s %-18s %-30s %-30s\n",
		"#", "ACTION", "KIND", "FROM", "TO")
	for _, r := range resp.Rows {
		from := fmt.Sprintf("%s/%s", r.FromKind, r.FromName)
		to := fmt.Sprintf("%s/%s", r.ToKind, r.ToName)
		fmt.Fprintf(w, "%-3d %-9s %-18s %-30s %-30s\n",
			r.Index, r.Action, truncate(r.Kind, 18),
			truncate(from, 30), truncate(to, 30))
		if len(r.Superseded) > 0 {
			fmt.Fprintf(w, "        supersedes auto edge(s): %s\n",
				strings.Join(r.Superseded, ", "))
		}
		if len(r.Conflicts) > 0 {
			fmt.Fprintf(w, "        conflicts with edge(s): %s\n",
				strings.Join(r.Conflicts, ", "))
		}
	}
	if resp.DryRun {
		fmt.Fprintln(w, "\nRun without --dry-run to apply.")
	}
}
