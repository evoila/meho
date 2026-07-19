// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"context"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// newCollectionsDeleteCmd returns the `meho docs collections delete` command.
//
// #2487. The delete half of the doc-collection registry: it calls
// DELETE /api/v1/doc_collections/<key> to deregister a disabled,
// tenant-owned collection and free its collection_key for re-creation.
// `disable` only hides a collection from search (the row and its occupied
// key persist), so a collection mis-registered under the wrong backend
// could never be fixed under its own key; delete closes that recovery gap.
// tenant_admin only (parity with create / probe / enable / disable).
//
// Two server-side guards surface as typed errors (renderHTTPStatus maps
// them distinctly): a global (platform-owned) row → 403 `global_collection`,
// and a still-enabled collection → 409 `collection_not_disabled`. The
// disable → delete two-step is deliberate: disabling first keeps the typed
// `collection_disabled` search rejection as an operator-visible warning
// window before the key 404s.
func newCollectionsDeleteCmd() *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <collection-key>",
		Short: "Deregister a disabled, tenant-owned doc collection (tenant_admin)",
		Long: "delete calls DELETE /api/v1/doc_collections/<key> to " +
			"deregister a disabled, tenant-owned collection and free its " +
			"collection key for re-creation — the recovery path when a " +
			"collection was registered under the wrong backend. `disable` " +
			"only hides a collection from search; delete removes the row so " +
			"the key can be re-`create`d.\n\n" +
			"The collection must be disabled first (run `meho docs " +
			"collections disable <key>`); a still-enabled collection is " +
			"refused with a 409 so in-flight searchers get a terminal " +
			"`collection_disabled` warning window before the key disappears. " +
			"A global (platform-owned) collection cannot be deleted by a " +
			"tenant admin (403). tenant_admin only.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCollectionDelete(cmd, lifecycleOptions{
				CollectionKey:     args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a JSON status envelope")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runCollectionDelete(cmd *cobra.Command, opts lifecycleOptions) error {
	backplaneURL, gateErr := gateLifecycle(cmd, opts)
	if gateErr != nil {
		return gateErr
	}
	resp, err := deleteCollection(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if resp.StatusCode() != http.StatusNoContent {
		return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), map[string]string{
			"collection_key": opts.CollectionKey,
			"status":         "deleted",
		})
	}
	fmt.Fprintf(cmd.OutOrStdout(),
		"collection %q deleted; its key is free to re-create\n", opts.CollectionKey)
	return nil
}

func deleteCollection(
	ctx context.Context,
	backplaneURL string,
	opts lifecycleOptions,
) (*api.DeleteCollectionEndpointApiV1DocCollectionsCollectionKeyDeleteResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.DeleteCollectionEndpointApiV1DocCollectionsCollectionKeyDeleteResponse, error) {
			return authed.DeleteCollectionEndpointApiV1DocCollectionsCollectionKeyDeleteWithResponse(
				ctx,
				opts.CollectionKey,
				&api.DeleteCollectionEndpointApiV1DocCollectionsCollectionKeyDeleteParams{},
			)
		},
		func(r *api.DeleteCollectionEndpointApiV1DocCollectionsCollectionKeyDeleteResponse) int {
			return r.StatusCode()
		},
	)
}
