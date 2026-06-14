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

// newEnableReadsCmd returns the `meho connector enable-reads` command.
//
// CLI shape:
//
//	meho connector enable-reads <connector_id> [--confirm] [--json] [--backplane <url>]
//
// Hits POST /api/v1/connectors/<connector_id>/enable-reads, which
// flips is_enabled=true on every ingested op whose HTTP method is GET
// or HEAD in one pass — the bulk read-class enable path (G0.25-T7
// #1749). Every write-shaped verb (POST / PUT / PATCH / DELETE) stays
// default-deny. Unlike `enable`, this does not move any group's
// review_status; it is a per-op flip. tenant_admin role required.
//
// The route returns 200 with {connector_id, ops_enabled}; the verb
// renders the count so the operator sees how many ops became
// dispatchable. The route is idempotent: a re-run once the reads are
// enabled returns ops_enabled=0.
func newEnableReadsCmd() *cobra.Command {
	var (
		confirmFlag       bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "enable-reads <connector_id>",
		Short: "Bulk-enable every read-class (GET/HEAD) op; writes stay default-deny",
		Long: "enable-reads calls POST /api/v1/connectors/<connector_id>/enable-reads.\n\n" +
			"It flips is_enabled=true on every ingested operation whose HTTP method\n" +
			"is GET or HEAD, in one pass, leaving every write-shaped verb\n" +
			"(POST / PUT / PATCH / DELETE) default-deny. Use it to stand up broad\n" +
			"governed READ coverage on a large ingested surface without editing each\n" +
			"op individually; writes keep their per-op / composite curation.\n\n" +
			"Unlike `enable`, this does NOT move any group's review_status — it is a\n" +
			"per-op flip. The route is idempotent: re-running once the reads are\n" +
			"enabled flips nothing and reports 0.\n\n" +
			"Without --confirm, the verb prompts on stdin for confirmation;\n" +
			"--confirm skips the prompt for scripted use (CI pipelines, etc.).\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEnableReads(cmd, enableReadsOptions{
				ConnectorID:       args[0],
				Confirm:           confirmFlag,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&confirmFlag, "confirm", false,
		"skip the interactive confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type enableReadsOptions struct {
	ConnectorID       string
	Confirm           bool
	JSONOut           bool
	BackplaneOverride string
}

// enableReadsResult is the --json envelope the verb renders. It
// mirrors the backplane's EnableReadsResponse so a `--json` pipeline
// sees the same shape the route returned (connector_id + the count of
// read-class ops flipped).
type enableReadsResult struct {
	ConnectorID string `json:"connector_id"`
	OpsEnabled  int    `json:"ops_enabled"`
}

func runEnableReads(cmd *cobra.Command, opts enableReadsOptions) error {
	// Prompt before resolving the backplane so the operator's only
	// interaction is the confirmation — if they hit 'n', they haven't
	// made a network call yet.
	if !opts.Confirm {
		prompt := fmt.Sprintf(
			"Bulk-enable every read-class (GET/HEAD) op on connector %s — they become "+
				"dispatchable. Writes stay default-deny. Continue?",
			opts.ConnectorID,
		)
		if !confirm(cmd, prompt) {
			fmt.Fprintln(cmd.OutOrStdout(), "Aborted.")
			return errTransitionAborted
		}
	}
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	opsEnabled, err := postEnableReads(cmd.Context(), backplaneURL, opts.ConnectorID)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	result := enableReadsResult{ConnectorID: opts.ConnectorID, OpsEnabled: opsEnabled}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printEnableReadsResult(cmd.OutOrStdout(), result)
	return nil
}

// postEnableReads drives the typed-client enable-reads endpoint with a
// one-shot 401-retry. On a 200 it returns the ops_enabled count; a
// non-2xx surfaces as *httpResponseError for the caller to route
// through renderHTTPStatus. A 200 with a missing/empty JSON200 body
// (contract drift) is surfaced as an error so the verb never reports a
// silent zero count as success.
func postEnableReads(
	ctx context.Context,
	backplaneURL string,
	connectorID string,
) (int, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return 0, err
	}
	resp, rerr := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EnableReadsEndpointApiV1ConnectorsConnectorIdEnableReadsPostResponse, error) {
			return authed.EnableReadsEndpointApiV1ConnectorsConnectorIdEnableReadsPostWithResponse(
				ctx,
				connectorID,
				&api.EnableReadsEndpointApiV1ConnectorsConnectorIdEnableReadsPostParams{},
			)
		},
		func(r *api.EnableReadsEndpointApiV1ConnectorsConnectorIdEnableReadsPostResponse) int {
			return r.StatusCode()
		},
	)
	if rerr != nil {
		return 0, rerr
	}
	if resp.StatusCode() != http.StatusOK {
		return 0, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		// 200 with no decodable body — contract drift between the route
		// and the regenerated client. Surface it rather than report a
		// silent zero.
		return 0, fmt.Errorf("enable-reads returned HTTP 200 with no JSON body")
	}
	return resp.JSON200.OpsEnabled, nil
}

func printEnableReadsResult(w io.Writer, r enableReadsResult) {
	switch r.OpsEnabled {
	case 0:
		fmt.Fprintf(w, "enable-reads %s — no read-class ops to enable "+
			"(already enabled or none ingested)\n", r.ConnectorID)
	case 1:
		fmt.Fprintf(w, "enable-reads %s — enabled 1 read operation\n", r.ConnectorID)
	default:
		fmt.Fprintf(w, "enable-reads %s — enabled %d read operations\n", r.ConnectorID, r.OpsEnabled)
	}
}
