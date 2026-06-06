// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newCollectionsCmd returns the `meho docs collections` command tree.
//
// Two faces share the parent. The catalogue-discovery `list` verb (T4
// #1553) is a read every operator may run — it lists the collections the
// tenant is entitled to search. The lifecycle verbs (`probe` / `enable` /
// `disable`, T6 #1555) mutate the doc_collections row and require
// tenant_admin (the connector enable/disable gate).
//
// `provisioned` carries the meho-docs capability resolved at command-tree-
// build time; an unprovisioned tenant gets the typed
// `addon_not_provisioned` refusal before any network call on every verb,
// the same gate `meho docs search` enforces.
func newCollectionsCmd(provisioned bool) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "collections",
		Short: "List doc collections, and probe / toggle their backends",
		Long: "collections operates the doc-collection catalogue. `list` " +
			"(operator) shows the collections you are entitled to search — " +
			"the keys `meho docs search --collection` accepts. The lifecycle " +
			"verbs (tenant_admin) operate readiness: `probe` refreshes a " +
			"collection's cached liveness (doc count, last ingest, " +
			"readiness) from its backend and transitions its status; " +
			"`enable` / `disable` move a collection in / out of search " +
			"service. A managed-RAG index answers searches only once it is " +
			"built, and rebuilds serialize per project — `probe` surfaces " +
			"that as the collection's status rather than hiding it behind a " +
			"silent empty search result.",
		Hidden:       !provisioned,
		SilenceUsage: true,
	}
	cmd.AddCommand(newCollectionsListCmd(provisioned))
	cmd.AddCommand(newCollectionsProbeCmd(provisioned))
	cmd.AddCommand(newCollectionsEnableCmd(provisioned))
	cmd.AddCommand(newCollectionsDisableCmd(provisioned))
	return cmd
}

// lifecycleOptions is the shared flag/arg set for the collections verbs.
type lifecycleOptions struct {
	CollectionKey     string
	JSONOut           bool
	BackplaneOverride string
	// Provisioned carries the meho-docs capability gate. When false the
	// verb refuses with the typed addon_not_provisioned error before
	// touching the network.
	Provisioned bool
}

func newCollectionsProbeCmd(provisioned bool) *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "probe <collection-key>",
		Short: "Probe a collection's backend and refresh its cached liveness",
		Long: "probe calls POST /api/v1/doc_collections/<key>/probe: it " +
			"queries the collection's backend for index readiness, doc " +
			"count, and last-ingest time, persists them onto the " +
			"doc_collections row on success only (a failed probe leaves the " +
			"cached liveness untouched), and transitions the collection's " +
			"status (provisioning/rebuilding → ready once the index is " +
			"built). Renders the BackendReadiness result as a text table; " +
			"--json emits the raw payload. A 503 means the backend is " +
			"unavailable; the row is unchanged.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCollectionProbe(cmd, lifecycleOptions{
				CollectionKey:     args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				Provisioned:       provisioned,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw BackendReadiness JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func newCollectionsEnableCmd(provisioned bool) *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "enable <collection-key>",
		Short: "Return a disabled collection to search service",
		Long: "enable calls POST /api/v1/doc_collections/<key>/enable, " +
			"moving a disabled collection back to provisioning (a follow-up " +
			"`probe` promotes it to ready once its index confirms). " +
			"Idempotent: re-enabling a live collection is a no-op. A 409 " +
			"means the move is forbidden from the collection's current state.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCollectionEnable(cmd, lifecycleOptions{
				CollectionKey:     args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				Provisioned:       provisioned,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a JSON status envelope")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func newCollectionsDisableCmd(provisioned bool) *cobra.Command {
	var (
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "disable <collection-key>",
		Short: "Hide a collection from search service",
		Long: "disable calls POST /api/v1/doc_collections/<key>/disable, " +
			"moving the collection to disabled so search_docs fails typed " +
			"(403) against it rather than returning an empty result. " +
			"Idempotent: re-disabling an already-disabled collection is a " +
			"no-op.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runCollectionDisable(cmd, lifecycleOptions{
				CollectionKey:     args[0],
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
				Provisioned:       provisioned,
			})
		},
	}
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit a JSON status envelope")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

// gateLifecycle runs the shared pre-flight every collections verb needs:
// the capability gate, a non-empty key, and backplane resolution. Returns
// the resolved URL or a rendered error the caller returns directly.
func gateLifecycle(cmd *cobra.Command, opts lifecycleOptions) (string, error) {
	if !opts.Provisioned {
		return "", errNotProvisioned(cmd, opts.JSONOut)
	}
	if opts.CollectionKey == "" {
		return "", output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("a non-empty <collection-key> argument is required"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return "", output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	return backplaneURL, nil
}

func runCollectionProbe(cmd *cobra.Command, opts lifecycleOptions) error {
	backplaneURL, gateErr := gateLifecycle(cmd, opts)
	if gateErr != nil {
		return gateErr
	}
	resp, err := probeCollection(cmd.Context(), backplaneURL, opts)
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
				"call %s: HTTP 200 without a BackendReadiness payload",
				backplaneURL,
			)),
			opts.JSONOut,
		)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
	}
	printReadiness(cmd.OutOrStdout(), opts.CollectionKey, resp.JSON200)
	return nil
}

func runCollectionEnable(cmd *cobra.Command, opts lifecycleOptions) error {
	backplaneURL, gateErr := gateLifecycle(cmd, opts)
	if gateErr != nil {
		return gateErr
	}
	resp, err := enableCollection(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	return renderLifecycleResult(cmd, backplaneURL, opts, resp.StatusCode(), resp.Body, "enabled")
}

func runCollectionDisable(cmd *cobra.Command, opts lifecycleOptions) error {
	backplaneURL, gateErr := gateLifecycle(cmd, opts)
	if gateErr != nil {
		return gateErr
	}
	resp, err := disableCollection(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	return renderLifecycleResult(cmd, backplaneURL, opts, resp.StatusCode(), resp.Body, "disabled")
}

// renderLifecycleResult maps the 204 success of an enable/disable call to
// the verb's confirmation output, deferring every non-204 status to the
// shared HTTP-status renderer (404 / 409 / 403 / 401). The route returns
// 204 on both the transition and the idempotent no-op, so the
// confirmation reads "is now <action>" without claiming a write happened.
func renderLifecycleResult(
	cmd *cobra.Command,
	backplaneURL string,
	opts lifecycleOptions,
	statusCode int,
	body []byte,
	action string,
) error {
	if statusCode != http.StatusNoContent {
		return renderHTTPStatus(cmd, backplaneURL, statusCode, body, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), map[string]string{
			"collection_key": opts.CollectionKey,
			"status":         action,
		})
	}
	fmt.Fprintf(cmd.OutOrStdout(), "collection %q is now %s\n", opts.CollectionKey, action)
	return nil
}

func probeCollection(
	ctx context.Context,
	backplaneURL string,
	opts lifecycleOptions,
) (*api.ProbeCollectionEndpointApiV1DocCollectionsCollectionKeyProbePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.ProbeCollectionEndpointApiV1DocCollectionsCollectionKeyProbePostResponse, error) {
			return authed.ProbeCollectionEndpointApiV1DocCollectionsCollectionKeyProbePostWithResponse(
				ctx,
				opts.CollectionKey,
				&api.ProbeCollectionEndpointApiV1DocCollectionsCollectionKeyProbePostParams{},
			)
		},
		func(r *api.ProbeCollectionEndpointApiV1DocCollectionsCollectionKeyProbePostResponse) int {
			return r.StatusCode()
		},
	)
}

func enableCollection(
	ctx context.Context,
	backplaneURL string,
	opts lifecycleOptions,
) (*api.EnableCollectionEndpointApiV1DocCollectionsCollectionKeyEnablePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EnableCollectionEndpointApiV1DocCollectionsCollectionKeyEnablePostResponse, error) {
			return authed.EnableCollectionEndpointApiV1DocCollectionsCollectionKeyEnablePostWithResponse(
				ctx,
				opts.CollectionKey,
				&api.EnableCollectionEndpointApiV1DocCollectionsCollectionKeyEnablePostParams{},
			)
		},
		func(r *api.EnableCollectionEndpointApiV1DocCollectionsCollectionKeyEnablePostResponse) int {
			return r.StatusCode()
		},
	)
}

func disableCollection(
	ctx context.Context,
	backplaneURL string,
	opts lifecycleOptions,
) (*api.DisableCollectionEndpointApiV1DocCollectionsCollectionKeyDisablePostResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	return retryOn401(ctx, authed,
		func(ctx context.Context) (*api.DisableCollectionEndpointApiV1DocCollectionsCollectionKeyDisablePostResponse, error) {
			return authed.DisableCollectionEndpointApiV1DocCollectionsCollectionKeyDisablePostWithResponse(
				ctx,
				opts.CollectionKey,
				&api.DisableCollectionEndpointApiV1DocCollectionsCollectionKeyDisablePostParams{},
			)
		},
		func(r *api.DisableCollectionEndpointApiV1DocCollectionsCollectionKeyDisablePostResponse) int {
			return r.StatusCode()
		},
	)
}

// printReadiness renders a BackendReadiness as a compact key/value block.
// REACHABLE and INDEX BUILT are the two booleans that decide whether a
// search will answer; DOC COUNT and LAST INGEST are the liveness an
// operator reads to confirm an ingest landed. A nil doc count / last
// ingest renders as "-" so an absent value is not misread as zero.
func printReadiness(w io.Writer, collectionKey string, r *api.BackendReadiness) {
	fmt.Fprintf(w, "collection:   %s\n", collectionKey)
	fmt.Fprintf(w, "reachable:    %t\n", r.Reachable)
	fmt.Fprintf(w, "index built:  %t\n", r.IndexBuilt)
	fmt.Fprintf(w, "doc count:    %s\n", formatDocCount(r.DocCount))
	fmt.Fprintf(w, "last ingest:  %s\n", formatLastIngest(r.LastIngestedAt))
}

// formatDocCount renders the optional doc count, which is *int on the
// wire (a backend that does not expose a count sends null). A nil count
// renders as "-" so it isn't misread as 0.
func formatDocCount(count *int) string {
	if count == nil {
		return "-"
	}
	return fmt.Sprintf("%d", *count)
}

// formatLastIngest renders the optional last-ingest timestamp in RFC 3339,
// or "-" when the backend did not report one.
func formatLastIngest(ts *time.Time) string {
	if ts == nil {
		return "-"
	}
	return ts.UTC().Format(time.RFC3339)
}
