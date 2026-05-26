// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// CatalogEntry mirrors the backend ConnectorSpecEntry
// (operations/ingest/catalog.py) as served by
// GET /api/v1/connectors/catalog (Goal #214 raw-REST ingest on-ramp;
// #743). Upstream is nil for typed connectors (no ingestable spec) —
// the `ingest --catalog` path refuses those rather than POSTing an
// empty specs list. SpecInfoVersion / SHA256 are pointers so the JSON
// null (empirical, not-yet-smoke-tested) round-trips distinctly from
// an empty string.
type CatalogEntry struct {
	Product                string   `json:"product"`
	Version                string   `json:"version"`
	ImplID                 string   `json:"impl_id"`
	RequiresConnectorClass string   `json:"requires_connector_class"`
	Upstream               []string `json:"upstream"`
	SpecInfoVersion        *string  `json:"spec_info_version"`
	SHA256                 *string  `json:"sha256"`
	Notes                  string   `json:"notes"`
}

// CatalogResponse is the envelope for GET /api/v1/connectors/catalog.
// Wrapped in {"catalog": [...]} so future paging fields can land
// non-breakingly, mirroring the GET / list shape.
type CatalogResponse struct {
	Catalog []CatalogEntry `json:"catalog"`
}

// newCatalogCmd returns the `meho connector catalog` parent command.
// The catalog is the curated map of (product, version) -> recommended
// OpenAPI spec source(s) + the registered connector class that covers
// the version label; it is the operator on-ramp for the generic-
// ingestion half of the two-layer connector model.
func newCatalogCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "catalog",
		Short:        "Curated connector-spec catalog (the raw-REST ingest on-ramp)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newCatalogListCmd())
	return cmd
}

// newCatalogListCmd returns the `meho connector catalog list` command.
//
// CLI shape:
//
//	meho connector catalog list [--json] [--backplane <url>]
//
// Hits GET /api/v1/connectors/catalog and renders one row per entry.
// The `registered` column is a best-effort cross-reference against
// GET /api/v1/connectors (which unions the class-side registry per T5
// #733): an entry whose (product, version, impl_id) appears there has
// its connector class registered. operator role suffices (read-only).
func newCatalogListCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List curated connector-spec catalog entries",
		Long: "list calls GET /api/v1/connectors/catalog and renders one row\n" +
			"per curated (product, version) entry: the impl_id, the connector\n" +
			"class that covers the version label, whether that class is\n" +
			"registered on this backplane, the observed spec.info.version (when\n" +
			"a spec has been ingest-verified), and operator notes.\n\n" +
			"Entries with an empty upstream are typed connectors with no\n" +
			"ingestable spec; the rest are generic-ingestable via\n" +
			"`meho connector ingest --catalog <product>/<version>`.\n\n" +
			"Read-only; operator role suffices.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runCatalogList(cmd, catalogListOptions{
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human table")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type catalogListOptions struct {
	JSONOut           bool
	BackplaneOverride string
}

func runCatalogList(cmd *cobra.Command, opts catalogListOptions) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	catalog, err := getCatalog(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), catalog)
	}
	// Best-effort registry cross-reference; a nil map degrades the
	// `registered` column to "?" rather than failing the listing.
	registered := registeredTriples(cmd.Context(), backplaneURL)
	printCatalogTable(cmd.OutOrStdout(), catalog, registered)
	return nil
}

func getCatalog(ctx context.Context, backplaneURL string) (*CatalogResponse, error) {
	raw, err := doAuthedRequest(ctx, backplaneURL, "GET", "/api/v1/connectors/catalog", nil)
	if err != nil {
		return nil, err
	}
	var out CatalogResponse
	if err := decodeJSON(raw, "connector catalog", &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// registeredTriples returns the set of (product, version, impl_id)
// the backplane reports as registered connectors, keyed via
// tripleKey. Best-effort: any error yields a nil map so the caller
// renders registration as unknown rather than failing the listing.
func registeredTriples(ctx context.Context, backplaneURL string) map[string]bool {
	list, err := getList(ctx, backplaneURL, "all")
	if err != nil {
		return nil
	}
	set := make(map[string]bool, len(list.Connectors))
	for _, c := range list.Connectors {
		set[tripleKey(c.Product, c.Version, c.ImplID)] = true
	}
	return set
}

// tripleKey joins the connector triple with a NUL separator so
// distinct (product, version, impl_id) tuples can't collide via
// string concatenation (e.g. ("a-b","c") vs ("a","b-c")).
func tripleKey(product, version, implID string) string {
	return product + "\x00" + version + "\x00" + implID
}

func printCatalogTable(w io.Writer, c *CatalogResponse, registered map[string]bool) {
	if len(c.Catalog) == 0 {
		fmt.Fprintln(w, "0 catalog entries")
		return
	}
	fmt.Fprintf(w, "%d catalog entr%s\n", len(c.Catalog), pluralY(len(c.Catalog)))
	fmt.Fprintf(w, "%-22s %-13s %-24s %-4s %-9s %s\n",
		"product/version", "impl_id", "connector_class", "reg", "spec_ver", "notes",
	)
	for _, e := range c.Catalog {
		fmt.Fprintf(w, "%-22s %-13s %-24s %-4s %-9s %s\n",
			truncate(e.Product+"/"+e.Version, 22),
			truncate(e.ImplID, 13),
			truncate(e.RequiresConnectorClass, 24),
			registeredLabel(registered, e),
			truncate(specVersionLabel(e), 9),
			truncate(e.Notes, 60),
		)
	}
}

// registeredLabel renders the registration column: "yes"/"no" when
// the cross-reference succeeded, "?" when it was unavailable.
func registeredLabel(registered map[string]bool, e CatalogEntry) string {
	if registered == nil {
		return "?"
	}
	if registered[tripleKey(e.Product, e.Version, e.ImplID)] {
		return "yes"
	}
	return "no"
}

// specVersionLabel renders the observed spec.info.version, or "-" when
// the entry has not been ingest-verified yet (the common case for a
// fresh catalog).
func specVersionLabel(e CatalogEntry) string {
	if e.SpecInfoVersion != nil && *e.SpecInfoVersion != "" {
		return *e.SpecInfoVersion
	}
	return "-"
}

func pluralY(n int) string {
	if n == 1 {
		return "y"
	}
	return "ies"
}

// Catalog resolution moved to the backplane in G0.14-T9 (#1150).
// The CLI's `--catalog <product>/<version>` flag is now a thin shell
// that POSTs `{"catalog_entry": "<product>/<version>"}` directly; the
// backplane validates the reference, resolves the entry against the
// packaged catalog, and surfaces structured 422 envelopes per the
// T11 error-shape convention for the four failure modes
// (`catalog_entry_malformed`, `catalog_entry_not_found`,
// `catalog_entry_typed_connector`, `catalog_entry_templated_upstream`).
// See docs/cross-repo/connector-catalog.md.
