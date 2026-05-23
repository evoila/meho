// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package kb

import (
	"context"
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// ingestKbRequest mirrors the backend IngestKbRequest pydantic
// model. The substrate's `@model_validator(mode="after")` enforces
// "exactly one of `directory` / `tarball_url`"; the CLI only sets
// `directory` because `tarball_url` ingest returns 501 from the
// route in v0.2 (the substrate exposes only `ingest_directory`).
type ingestKbRequest struct {
	Directory string `json:"directory"`
	DryRun    bool   `json:"dry_run,omitempty"`
}

// newIngestCmd returns the `meho kb ingest` command.
//
// CLI shape (per issue #418):
//
//	meho kb ingest <directory> [--dry-run] [--json] [--backplane <url>]
//
// Role: tenant_admin. Operator-role JWT lands as 403
// insufficient_role.
//
// The directory path is **interpreted on the backplane host**, not
// the operator's workstation — the substrate walks the supplied path
// via `KbService.ingest_directory`. Operators running the CLI
// against a remote backplane must therefore stage their kb/ tree on
// the backplane host (or run the CLI on the backplane host
// itself); the route does not accept tarball uploads in v0.2 (that
// path is filed as v0.2.next under Initiative #331 and currently
// returns 501 when `tarball_url` is set).
//
// `--dry-run` short-circuits the substrate's write path; the
// counters in the returned `IngestionResult` reflect what _would_
// have been inserted / updated / skipped / errored.
//
// Exit codes:
//   - 0   ingest returned cleanly; counters rendered
//   - 2   auth_expired
//   - 3   unreachable
//   - 4   unexpected_response (incl. 400 directory_not_found /
//     not_a_directory, 422 schema-violation, 501 tarball-not-supported)
//   - 5   insufficient_role
func newIngestCmd() *cobra.Command {
	var (
		dryRun            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "ingest <directory>",
		Short: "Bulk-ingest a kb/ directory on the backplane host (tenant_admin)",
		Long: "ingest calls POST /api/v1/kb/ingest. Tenant_admin only — " +
			"operator-role JWT lands as 403 insufficient_role.\n\n" +
			"The directory path is interpreted on the backplane host, " +
			"not the operator's workstation — the substrate walks the " +
			"supplied path via KbService.ingest_directory. Operators " +
			"running the CLI against a remote backplane must stage " +
			"their kb/ tree on the backplane host (or run the CLI on " +
			"the backplane host itself).\n\n" +
			"--dry-run short-circuits the substrate's write path; the " +
			"counters in the returned IngestionResult reflect what " +
			"would have been inserted / updated / skipped / errored " +
			"without actually writing.\n\n" +
			"The substrate's body-hash short-circuit means re-ingesting " +
			"an unchanged corpus only pays an updated_at bump per file; " +
			"the skipped_count counter rises rather than updated_count.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runIngest(cmd, ingestOptions{
				Directory:         args[0],
				DryRun:            dryRun,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"resolve the plan without writing to the substrate (counters reflect intent only)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit raw IngestionResult JSON instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type ingestOptions struct {
	Directory         string
	DryRun            bool
	JSONOut           bool
	BackplaneOverride string
}

func runIngest(cmd *cobra.Command, opts ingestOptions) error {
	if opts.Directory == "" {
		return output.RenderError(
			cmd.ErrOrStderr(),
			output.Unexpected("ingest requires a non-empty <directory> argument"),
			opts.JSONOut,
		)
	}
	backplaneURL, err := backplane.Resolve(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), opts.JSONOut)
	}
	result, err := postIngest(cmd.Context(), backplaneURL, opts)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	printIngestSummary(cmd.OutOrStdout(), result, opts.DryRun)
	return nil
}

func postIngest(ctx context.Context, backplaneURL string, opts ingestOptions) (*IngestionResult, error) {
	body := ingestKbRequest{Directory: opts.Directory, DryRun: opts.DryRun}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal kb ingest request: %w", err)
	}
	resp, err := doAuthedRequest(ctx, backplaneURL, "POST", "/api/v1/kb/ingest", raw)
	if err != nil {
		return nil, err
	}
	var out IngestionResult
	if err := json.Unmarshal(resp, &out); err != nil {
		return nil, fmt.Errorf("decode kb ingest response: %w", err)
	}
	return &out, nil
}

// printIngestSummary renders the four-bucket counter result as a
// stable key-value summary. Errors (if any) are appended one per
// line so an operator triaging a partial-failure run sees every
// file path that failed without needing --json.
func printIngestSummary(w io.Writer, r *IngestionResult, dryRun bool) {
	if r == nil {
		return
	}
	if dryRun {
		fmt.Fprintln(w, "dry-run: substrate write path skipped; counters reflect intent only")
	}
	fmt.Fprintf(w, "%-14s %d\n", "inserted:", r.InsertedCount)
	fmt.Fprintf(w, "%-14s %d\n", "updated:", r.UpdatedCount)
	fmt.Fprintf(w, "%-14s %d\n", "skipped:", r.SkippedCount)
	fmt.Fprintf(w, "%-14s %d\n", "errored:", r.ErrorCount)
	total := r.InsertedCount + r.UpdatedCount + r.SkippedCount + r.ErrorCount
	fmt.Fprintf(w, "%-14s %d\n", "total:", total)
	if len(r.Errors) > 0 {
		fmt.Fprintln(w, "errors:")
		for _, e := range r.Errors {
			fmt.Fprintf(w, "  - %s\n", e)
		}
	}
}
