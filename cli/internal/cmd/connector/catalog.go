// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

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
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
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

// getCatalog drives the typed-client catalog endpoint with a one-shot
// 401-retry. The catalog endpoint declares
// `response_model=CatalogListResponse`, so JSON200 lands as the
// typed envelope; non-2xx surfaces as *httpResponseError for the
// caller to route through renderHTTPStatus.
func getCatalog(ctx context.Context, backplaneURL string) (*api.CatalogListResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.CatalogEndpointApiV1ConnectorsCatalogGetResponse, error) {
			return authed.CatalogEndpointApiV1ConnectorsCatalogGetWithResponse(ctx, &api.CatalogEndpointApiV1ConnectorsCatalogGetParams{})
		},
		func(r *api.CatalogEndpointApiV1ConnectorsCatalogGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		return nil, fmt.Errorf("backplane returned 200 OK but no JSON body decoded against CatalogListResponse")
	}
	return resp.JSON200, nil
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

func printCatalogTable(w io.Writer, c *api.CatalogListResponse, registered map[string]bool) {
	if len(c.Catalog) == 0 {
		fmt.Fprintln(w, "0 catalog entries")
		return
	}
	fmt.Fprintf(w, "%d catalog entr%s\n", len(c.Catalog), pluralY(len(c.Catalog)))
	fmt.Fprintf(w, "%-22s %-13s %-24s %-4s %-9s %-9s %s\n",
		"product/version", "impl_id", "connector_class", "reg", "spec_ver", "ships", "notes",
	)
	for _, e := range c.Catalog {
		fmt.Fprintf(w, "%-22s %-13s %-24s %-4s %-9s %-9s %s\n",
			truncate(e.Product+"/"+e.Version, 22),
			truncate(e.ImplId, 13),
			truncate(e.RequiresConnectorClass, 24),
			registeredLabel(registered, e),
			truncate(specVersionLabel(e), 9),
			shippedResourceLabel(e),
			truncate(strDerefAny(e.Notes), 60),
		)
	}
}

// registeredLabel renders the registration column: "yes"/"no" when
// the cross-reference succeeded, "?" when it was unavailable.
func registeredLabel(registered map[string]bool, e api.ConnectorSpecEntry) string {
	if registered == nil {
		return "?"
	}
	if registered[tripleKey(e.Product, e.Version, e.ImplId)] {
		return "yes"
	}
	return "no"
}

// specVersionLabel renders the observed spec.info.version, or "-" when
// the entry has not been ingest-verified yet (the common case for a
// fresh catalog).
func specVersionLabel(e api.ConnectorSpecEntry) string {
	if e.SpecInfoVersion != nil && *e.SpecInfoVersion != "" {
		return *e.SpecInfoVersion
	}
	return "-"
}

// shippedResourceLabel renders the `ships` column: whether the entry
// ships a MEHO-authored OpenAPI spec and/or ExecutionProfile as
// package data, so catalog-driven ingest fills the bytes locally
// instead of fetching from `upstream` / requiring an upload. The two
// resource fields are independent (#1975), so all four combinations
// are distinguished:
//
//	spec+prof  ships both a local spec and a profile
//	spec       ships a local spec only (fetch/author the profile)
//	prof       ships a local profile only
//	-          ships neither (the upstream-fetch / upload on-ramp)
func shippedResourceLabel(e api.ConnectorSpecEntry) string {
	spec := nonBlank(e.SpecResource)
	prof := nonBlank(e.ProfileResource)
	switch {
	case spec && prof:
		return "spec+prof"
	case spec:
		return "spec"
	case prof:
		return "prof"
	default:
		return "-"
	}
}

// nonBlank reports whether an optional string field is present and not
// empty. The catalog `spec_resource`/`profile_resource` fields are
// `Optional[str]` defaulting to null; the backend rejects blank values
// at validation, but the CLI treats nil and "" identically to stay
// robust to either shape.
func nonBlank(s *string) bool {
	return s != nil && *s != ""
}

// strDerefAny returns the pointee or empty when nil. Used for the
// generated client's `*string` optional fields (e.g. `Notes` on
// `ConnectorSpecEntry`).
func strDerefAny(s *string) string {
	if s == nil {
		return ""
	}
	return *s
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
