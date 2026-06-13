// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCollectionsCreateCmd returns the `meho docs collections create` command.
//
// #1739. The write half of the doc-collection registry: it calls
// POST /api/v1/doc_collections to register a new collection so
// `meho docs search --collection` (and the MCP / agent fronts) can route
// to it — replacing the raw `INSERT INTO doc_collections` an operator
// would otherwise run. tenant_admin only (parity with probe / enable /
// disable); tenant-scoped server-side (the collection lands in the
// operator's own tenant, never a flag value).
//
// Two input shapes:
//   - flags: --vendor / --product / --backend-type / --backend-ref (+ the
//     <collection-key> positional). The everyday single-collection path.
//   - --from-file <path>: a JSON object matching the DocCollectionCreate
//     body, for an operator who already has the row described as data. The
//     CLI `import`-style on-ramp (mirrors `meho targets import`'s file
//     read) without a bulk format — one collection per call.
//
// Exit codes mirror the sibling collections verbs (renderHTTPStatus maps
// 422 → unexpected with the unknown-backend-type detail, 409 → unexpected
// with the conflict detail, 403 → insufficient_role).
func newCollectionsCreateCmd(provisioned bool) *cobra.Command {
	var opts createCollectionOptions
	cmd := &cobra.Command{
		Use:   "create <collection-key>",
		Short: "Register a new doc collection (tenant_admin)",
		Long: "create calls POST /api/v1/doc_collections to register a new " +
			"documentation collection in your tenant so `meho docs search " +
			"--collection <key>` can route to it — the audited, validated " +
			"alternative to a raw database INSERT. It seeds identity + the " +
			"backend binding only; it does not trigger ingest or an index " +
			"rebuild — the new collection starts in `provisioning` and a " +
			"follow-up `probe` promotes it to `ready` once its index " +
			"confirms.\n\n" +
			"Supply the fields with flags (--vendor, --product, " +
			"--backend-type, --backend-ref) plus the <collection-key> " +
			"positional, or pass --from-file <path> with a JSON body " +
			"matching the create schema. The backend type must be a " +
			"registered search backend (e.g. corpus-http); an unregistered " +
			"type is rejected up front (422) rather than committing an " +
			"unroutable row. A collection key that already exists in your " +
			"scope is a conflict (409). tenant_admin only.",
		Args:          cobra.MaximumNArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 1 {
				opts.CollectionKey = args[0]
			}
			opts.Provisioned = provisioned
			return runCollectionCreate(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.Vendor, "vendor", "",
		"vendor the corpus covers (e.g. 'VMware by Broadcom')")
	cmd.Flags().StringSliceVar(&opts.Products, "product", nil,
		"product the corpus covers (repeatable, e.g. --product vsphere --product nsx)")
	cmd.Flags().StringVar(&opts.Description, "description", "",
		"optional free-text description")
	cmd.Flags().StringVar(&opts.WhenToUse, "when-to-use", "",
		"optional 'pick this collection when…' blurb surfaced to agents")
	cmd.Flags().StringVar(&opts.BackendType, "backend-type", "",
		"search-backend type to route to (e.g. corpus-http)")
	cmd.Flags().StringVar(&opts.BackendRef, "backend-ref", "",
		"backend config as a JSON object (e.g. '{\"endpoint\":\"https://corpus/v1/search\"}')")
	cmd.Flags().StringVar(&opts.FromFile, "from-file", "",
		"read the full create body from a JSON file instead of the flags")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false,
		"emit the created collection as JSON instead of a confirmation line")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// createCollectionOptions is the flag/arg set for the create verb.
type createCollectionOptions struct {
	CollectionKey     string
	Vendor            string
	Products          []string
	Description       string
	WhenToUse         string
	BackendType       string
	BackendRef        string
	FromFile          string
	JSONOut           bool
	BackplaneOverride string
	Provisioned       bool
}

func runCollectionCreate(cmd *cobra.Command, opts createCollectionOptions) error {
	if !opts.Provisioned {
		return errNotProvisioned(cmd, opts.JSONOut)
	}
	body, buildErr := buildCreateBody(opts)
	if buildErr != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(buildErr.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := createCollection(cmd.Context(), backplaneURL, body)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusCreated {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON201 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 201 without a DocCollection payload", backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), *resp.JSON201)
	}
	fmt.Fprintf(cmd.OutOrStdout(),
		"collection %q created (status %s); run `meho docs collections probe %s` to promote it\n",
		resp.JSON201.CollectionKey, resp.JSON201.Status, resp.JSON201.CollectionKey)
	return nil
}

// buildCreateBody assembles the typed DocCollectionCreate body from either
// --from-file or the individual flags. Exposed for tests so the
// flag→body wiring (and the mutually-exclusive --from-file path) stays
// unit-checkable without standing up an httptest.Server.
func buildCreateBody(opts createCollectionOptions) (api.DocCollectionCreate, error) {
	if opts.FromFile != "" {
		return buildCreateBodyFromFile(opts)
	}
	return buildCreateBodyFromFlags(opts)
}

func buildCreateBodyFromFile(opts createCollectionOptions) (api.DocCollectionCreate, error) {
	var body api.DocCollectionCreate
	raw, err := os.ReadFile(opts.FromFile)
	if err != nil {
		return body, fmt.Errorf("read --from-file %s: %w", opts.FromFile, err)
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&body); err != nil {
		return body, fmt.Errorf("parse --from-file %s as a doc-collection create body: %w", opts.FromFile, err)
	}
	// A bare positional <collection-key> overrides the file's key only when
	// the file omitted it — otherwise the file is authoritative.
	if body.CollectionKey == "" {
		body.CollectionKey = opts.CollectionKey
	}
	if body.CollectionKey == "" {
		return body, fmt.Errorf("--from-file body is missing collection_key (and no <collection-key> argument was given)")
	}
	return body, nil
}

func buildCreateBodyFromFlags(opts createCollectionOptions) (api.DocCollectionCreate, error) {
	var body api.DocCollectionCreate
	if opts.CollectionKey == "" {
		return body, fmt.Errorf("a <collection-key> argument is required (or use --from-file)")
	}
	if opts.Vendor == "" {
		return body, fmt.Errorf("--vendor is required")
	}
	if opts.BackendType == "" {
		return body, fmt.Errorf("--backend-type is required (e.g. corpus-http)")
	}
	ref := map[string]interface{}{}
	if opts.BackendRef != "" {
		if err := json.Unmarshal([]byte(opts.BackendRef), &ref); err != nil {
			return body, fmt.Errorf("--backend-ref must be a JSON object: %w", err)
		}
	}
	body.CollectionKey = opts.CollectionKey
	body.Vendor = opts.Vendor
	body.Backend = api.DocCollectionBackend{Type: opts.BackendType, Ref: ref}
	if len(opts.Products) > 0 {
		products := append([]string(nil), opts.Products...)
		body.Products = &products
	}
	if opts.Description != "" {
		d := opts.Description
		body.Description = &d
	}
	if opts.WhenToUse != "" {
		w := opts.WhenToUse
		body.WhenToUse = &w
	}
	return body, nil
}

func createCollection(
	ctx context.Context,
	backplaneURL string,
	body api.DocCollectionCreate,
) (*api.CreateDocCollectionEndpointApiV1DocCollectionsPostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.CreateDocCollectionEndpointApiV1DocCollectionsPostResponse, error) {
			return authed.CreateDocCollectionEndpointApiV1DocCollectionsPostWithResponse(
				ctx,
				&api.CreateDocCollectionEndpointApiV1DocCollectionsPostParams{},
				body,
			)
		},
		func(r *api.CreateDocCollectionEndpointApiV1DocCollectionsPostResponse) int {
			return r.StatusCode()
		},
	)
}
