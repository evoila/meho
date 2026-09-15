// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCollectionsUpdateCmd returns the `meho docs collections update` command.
//
// #3601. The in-place repoint half of the doc-collection registry: it calls
// PATCH /api/v1/doc_collections/<key> to change a collection's mutable
// fields — primarily the backend `ref` endpoint — without a destructive
// delete + re-create. A migration-seeded collection carrying its own
// backend.ref endpoint keeps dialing the old one when a deployment moves its
// corpus (create conflicts on the key; delete refuses a global row), so
// search fails closed; this is the only governed in-place fix. tenant_admin
// only; a GLOBAL (platform-owned) row additionally needs platform_admin
// (403 `global_collection_update_forbidden`).
//
// Only the flags you set are sent (PATCH semantics), so an untouched field
// is never cleared. A `--backend-type` change is validated + endpoint-screened
// server-side exactly as create, and resets the collection to `provisioning`
// so a follow-up `probe` re-validates it against the new endpoint.
func newCollectionsUpdateCmd() *cobra.Command {
	var opts updateCollectionOptions
	cmd := &cobra.Command{
		Use:   "update <collection-key>",
		Short: "Repoint / update an existing doc collection in place (tenant_admin)",
		Long: "update calls PATCH /api/v1/doc_collections/<key> to change a " +
			"collection's mutable fields in place — primarily the backend " +
			"`ref` endpoint, so you can repoint a collection when its corpus " +
			"moves (e.g. http → https) instead of delete + re-create. Only " +
			"the flags you pass are changed; an untouched field is left as " +
			"is.\n\n" +
			"Repoint the endpoint with --backend-type + --backend-ref " +
			"(the backend is replaced as a whole, matching create); pass " +
			"--backend-type with no --backend-ref (or --backend-ref '{}') to " +
			"clear the ref so the deployment's global corpus URL applies. " +
			"Metadata changes: --description, --when-to-use, --product. Or " +
			"pass --from-file <path> with a JSON body of the fields to " +
			"change. A backend change is validated + endpoint-screened " +
			"(https + SSRF allowlist) exactly as create (422 on failure) and " +
			"resets the collection to `provisioning`, so run `meho docs " +
			"collections probe <key>` afterwards to promote it back to " +
			"`ready`.\n\n" +
			"tenant_admin only; a global (platform-owned) collection " +
			"additionally requires the platform_admin capability (403).",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			opts.CollectionKey = args[0]
			// Record which mutable flags the caller actually set so the
			// request body carries ONLY those keys (PATCH semantics): the
			// server distinguishes an absent field (leave as is) from an
			// explicit value, so sending a nil field as JSON null would
			// wrongly clear it.
			opts.setBackendType = cmd.Flags().Changed("backend-type")
			opts.setBackendRef = cmd.Flags().Changed("backend-ref")
			opts.setDescription = cmd.Flags().Changed("description")
			opts.setWhenToUse = cmd.Flags().Changed("when-to-use")
			opts.setProducts = cmd.Flags().Changed("product")
			return runCollectionUpdate(cmd, opts)
		},
	}
	cmd.Flags().StringVar(&opts.BackendType, "backend-type", "",
		"replacement search-backend type (e.g. corpus-http); the backend is replaced as a whole")
	cmd.Flags().StringVar(&opts.BackendRef, "backend-ref", "",
		"replacement backend config as a JSON object (e.g. '{\"endpoint\":\"https://corpus/v1/search\"}'); requires --backend-type; '{}' clears the ref")
	cmd.Flags().StringVar(&opts.Description, "description", "",
		"replacement free-text description")
	cmd.Flags().StringVar(&opts.WhenToUse, "when-to-use", "",
		"replacement 'pick this collection when…' blurb surfaced to agents")
	cmd.Flags().StringSliceVar(&opts.Products, "product", nil,
		"replacement product list (repeatable, e.g. --product vsphere --product nsx)")
	cmd.Flags().StringVar(&opts.FromFile, "from-file", "",
		"read the update body (fields to change) from a JSON file instead of the flags")
	cmd.Flags().BoolVar(&opts.JSONOut, "json", false,
		"emit the updated collection as JSON instead of a confirmation line")
	cmd.Flags().StringVar(&opts.BackplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// updateCollectionOptions is the flag/arg set for the update verb. The
// “set*“ booleans record which mutable flags the caller explicitly passed
// so the request body carries only those (PATCH semantics).
type updateCollectionOptions struct {
	CollectionKey     string
	BackendType       string
	BackendRef        string
	Description       string
	WhenToUse         string
	Products          []string
	FromFile          string
	JSONOut           bool
	BackplaneOverride string

	setBackendType bool
	setBackendRef  bool
	setDescription bool
	setWhenToUse   bool
	setProducts    bool
}

func runCollectionUpdate(cmd *cobra.Command, opts updateCollectionOptions) error {
	if opts.CollectionKey == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("a non-empty <collection-key> argument is required"),
			opts.JSONOut,
		)
	}
	bodyBytes, buildErr := buildUpdateBody(opts)
	if buildErr != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(buildErr.Error()), opts.JSONOut)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	resp, err := updateCollection(cmd.Context(), backplaneURL, opts.CollectionKey, bodyBytes)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusOK {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if resp.JSON200 == nil {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"call %s: HTTP 200 without a DocCollection payload", backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), *resp.JSON200)
	}
	col := resp.JSON200
	// A backend change resets the row to provisioning; hint the re-probe so
	// the operator restores it to `ready` (the create verb hints the same).
	if col.Status == "provisioning" {
		fmt.Fprintf(cmd.OutOrStdout(),
			"collection %q updated (status %s); run `meho docs collections probe %s` to re-validate it\n",
			col.CollectionKey, col.Status, col.CollectionKey)
		return nil
	}
	fmt.Fprintf(cmd.OutOrStdout(), "collection %q updated (status %s)\n", col.CollectionKey, col.Status)
	return nil
}

// buildUpdateBody assembles the PATCH request body from either --from-file
// or the individual flags, carrying ONLY the fields the caller set. Exposed
// for tests so the flag→body wiring (and the PATCH-only-set-fields contract)
// stays unit-checkable without an httptest.Server.
func buildUpdateBody(opts updateCollectionOptions) ([]byte, error) {
	if opts.FromFile != "" {
		return buildUpdateBodyFromFile(opts)
	}
	return buildUpdateBodyFromFlags(opts)
}

func buildUpdateBodyFromFlags(opts updateCollectionOptions) ([]byte, error) {
	body := map[string]any{}
	if opts.setBackendType {
		ref := map[string]any{}
		if opts.setBackendRef && opts.BackendRef != "" {
			if err := json.Unmarshal([]byte(opts.BackendRef), &ref); err != nil {
				return nil, fmt.Errorf("--backend-ref must be a JSON object: %w", err)
			}
		}
		body["backend"] = map[string]any{"type": opts.BackendType, "ref": ref}
	} else if opts.setBackendRef {
		return nil, fmt.Errorf("--backend-ref requires --backend-type (the backend is replaced as a whole)")
	}
	if opts.setDescription {
		body["description"] = opts.Description
	}
	if opts.setWhenToUse {
		body["when_to_use"] = opts.WhenToUse
	}
	if opts.setProducts {
		body["products"] = opts.Products
	}
	if len(body) == 0 {
		return nil, fmt.Errorf(
			"nothing to update: set at least one of --backend-type, --description, " +
				"--when-to-use, --product (or --from-file)")
	}
	return json.Marshal(body)
}

// buildUpdateBodyFromFile reads a JSON object of the fields to change and
// forwards only the allowed keys, verbatim. Unknown keys are rejected (a
// typo would otherwise be silently dropped) and an empty body is refused
// (the server would 422 it anyway; catch it locally with a clearer message).
func buildUpdateBodyFromFile(opts updateCollectionOptions) ([]byte, error) {
	raw, err := os.ReadFile(opts.FromFile)
	if err != nil {
		return nil, fmt.Errorf("read --from-file %s: %w", opts.FromFile, err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, fmt.Errorf("parse --from-file %s as a doc-collection update body: %w", opts.FromFile, err)
	}
	allowed := map[string]bool{
		"backend":     true,
		"description": true,
		"when_to_use": true,
		"products":    true,
	}
	for k := range fields {
		if !allowed[k] {
			return nil, fmt.Errorf(
				"--from-file body has an unsupported field %q (allowed: backend, description, when_to_use, products)",
				k)
		}
	}
	if len(fields) == 0 {
		return nil, fmt.Errorf(
			"--from-file body is empty; set at least one of backend, description, when_to_use, products")
	}
	return json.Marshal(fields)
}

func updateCollection(
	ctx context.Context,
	backplaneURL string,
	collectionKey string,
	bodyBytes []byte,
) (*api.UpdateDocCollectionEndpointApiV1DocCollectionsCollectionKeyPatchResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.UpdateDocCollectionEndpointApiV1DocCollectionsCollectionKeyPatchResponse, error) {
			return authed.UpdateDocCollectionEndpointApiV1DocCollectionsCollectionKeyPatchWithBodyWithResponse(
				ctx,
				collectionKey,
				&api.UpdateDocCollectionEndpointApiV1DocCollectionsCollectionKeyPatchParams{},
				"application/json",
				bytes.NewReader(bodyBytes),
			)
		},
		func(r *api.UpdateDocCollectionEndpointApiV1DocCollectionsCollectionKeyPatchResponse) int {
			return r.StatusCode()
		},
	)
}
