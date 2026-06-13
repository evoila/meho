// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// newIngestStatusCmd returns the `meho connector ingest-status`
// command — the post-`--no-wait` polling verb that closes the gap PR
// #1618 left open (Task #1621). Once an operator has detached an
// ingest with `meho connector ingest --no-wait`, or lost the waiting
// session, this verb re-attaches to the job:
//
//	meho connector ingest-status <job-id> [--wait] [--json] [--backplane <url>]
//
// The verb hits GET /api/v1/connectors/ingest/jobs/{job_id} —
// tenant_admin-gated, the same route `meho connector ingest`'s
// waiting path polls. It is the inverse of `ingest --no-wait`: a
// snapshot read by default, a poll-to-terminal with --wait.
//
// --wait is deliberately not --watch: `--watch` is this CLI's
// SSE follow-forever contract (`meho status --watch`); this is
// poll-to-terminal-then-exit, so it borrows `ingest`'s `--no-wait`
// vocabulary (compare `gh run view` vs `gh run watch`).
func newIngestStatusCmd() *cobra.Command {
	var (
		wait              bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "ingest-status <job-id>",
		Short: "Poll or inspect an async ingest job by id (after `ingest --no-wait`)",
		Long: "ingest-status reads GET /api/v1/connectors/ingest/jobs/<job-id>\n" +
			"and renders the job's current state — the post-`--no-wait`\n" +
			"polling verb. Use it after `meho connector ingest --no-wait`, or\n" +
			"when the waiting session was lost (Ctrl-C, dropped SSH), to check\n" +
			"a job again WITHOUT re-running ingest — a re-run starts a SECOND\n" +
			"job (the double-ingest failure mode the async handling prevents).\n\n" +
			"Default (no --wait): one GET, render a snapshot, exit. A still-\n" +
			"running job prints its identity + lifecycle (job_id, status,\n" +
			"request descriptors, started_at) and exits 0; a terminal job\n" +
			"renders exactly what the waiting-ingest path renders.\n\n" +
			"--wait: poll the job (2s cadence) until it leaves running, then\n" +
			"render the terminal shape — the inverse of `ingest --no-wait`.\n\n" +
			"Terminal rendering mirrors `meho connector ingest`: succeeded →\n" +
			"the connector summary (or the IngestResponse document on --json),\n" +
			"exit 0; failed/degraded → error_class + message (exit 4). The job\n" +
			"registry is process-local and bounded — a job lost to a pod\n" +
			"restart or eviction 404s; check `meho connector list` for the\n" +
			"connector before re-running ingest. Role: tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runIngestStatus(cmd, ingestStatusOptions{
				JobIDArg:          args[0],
				Wait:              wait,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().BoolVar(&wait, "wait", false,
		"poll the job (2s cadence) until it reaches a terminal status, then render the result; "+
			"without it, read one snapshot and exit (a running job exits 0 with its current state)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human render "+
			"(the raw IngestJobStatusResponse for a snapshot, the assembled IngestResponse on success)")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type ingestStatusOptions struct {
	JobIDArg          string
	Wait              bool
	JSONOut           bool
	BackplaneOverride string

	// pollInterval overrides defaultIngestPollInterval; zero means
	// "use the default". Unexported test seam so the --wait poll
	// tests don't sleep wall-clock seconds per iteration. Shared shape
	// with ingestOptions.pollInterval.
	pollInterval time.Duration
}

func runIngestStatus(cmd *cobra.Command, opts ingestStatusOptions) error {
	// The generated job-id path param is a uuid.UUID; parse CLI-side so
	// a typo'd id fails fast with an actionable message instead of
	// round-tripping to a backend 404/422.
	jobID, err := uuid.Parse(opts.JobIDArg)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"%q is not a valid ingest job id (expected a UUID, e.g. the job_id `meho connector ingest --no-wait` printed): %v",
				opts.JobIDArg, err)),
			opts.JSONOut,
		)
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}

	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}

	if opts.Wait {
		interval := opts.pollInterval
		if interval <= 0 {
			interval = defaultIngestPollInterval
		}
		fmt.Fprintf(cmd.ErrOrStderr(),
			"polling ingest job %s every %s until it reaches a terminal status\n", jobID, interval)
		st, perr := pollIngestJob(cmd.Context(), authed, jobID, interval)
		if perr != nil {
			return renderIngestStatusError(cmd, backplaneURL, jobID, perr, opts.JSONOut)
		}
		// pollIngestJob only returns on a non-running status, so this is
		// always a terminal render.
		return renderIngestTerminal(cmd, st, false, opts.JSONOut)
	}

	st, err := getIngestJob(cmd.Context(), authed, jobID)
	if err != nil {
		return renderIngestStatusError(cmd, backplaneURL, jobID, err, opts.JSONOut)
	}
	if st.Status == api.IngestJobStatusResponseStatusRunning {
		if opts.JSONOut {
			return output.PrintJSON(cmd.OutOrStdout(), st)
		}
		printIngestJobSnapshot(cmd.OutOrStdout(), st)
		return nil
	}
	// Any non-running status is terminal — render it exactly as the
	// waiting-ingest path does (succeeded summary / failed-degraded
	// error / loud undocumented), the shared lifecycle switch.
	return renderIngestTerminal(cmd, st, false, opts.JSONOut)
}

// getIngestJob reads one job-status row through the typed client with
// the package's one-shot 401-refresh, lifting a non-2xx status to a
// *httpResponseError for renderIngestStatusError to classify. The
// single-read sibling of pollIngestJob (which loops this same call).
func getIngestJob(
	ctx context.Context,
	authed *api.AuthedClient,
	jobID uuid.UUID,
) (*api.IngestJobStatusResponse, error) {
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.GetIngestJobEndpointApiV1ConnectorsIngestJobsJobIdGetResponse, error) {
			return authed.GetIngestJobEndpointApiV1ConnectorsIngestJobsJobIdGetWithResponse(
				ctx,
				jobID,
				&api.GetIngestJobEndpointApiV1ConnectorsIngestJobsJobIdGetParams{},
			)
		},
		func(r *api.GetIngestJobEndpointApiV1ConnectorsIngestJobsJobIdGetResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK {
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	if resp.JSON200 == nil {
		return nil, fmt.Errorf("backplane returned 200 OK on the job read but no JSON body decoded against IngestJobStatusResponse")
	}
	return resp.JSON200, nil
}

// renderIngestStatusError classifies a read/poll failure for the
// ingest-status verb. Same exit-code mapping as the ingest waiting
// path's renderIngestWaitError (401 → auth_expired / 403 →
// insufficient_role naming tenant_admin / 404 → the "job no longer
// tracked, check before re-running ingest" guidance), but phrased for
// the standalone verb: the operator is already polling a known job, so
// the guidance is "check `meho connector list`", not "don't retry the
// command you just ran".
func renderIngestStatusError(
	cmd *cobra.Command,
	backplaneURL string,
	jobID uuid.UUID,
	err error,
	jsonOut bool,
) error {
	var he *httpResponseError
	if errors.As(err, &he) {
		switch he.statusCode {
		case http.StatusUnauthorized:
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf(
					"backplane rejected the stored token while reading ingest job %s; run `meho login %s`",
					jobID, backplaneURL)),
				jsonOut,
			)
		case http.StatusForbidden:
			return output.RenderError(cmd.ErrOrStderr(),
				output.InsufficientRole(fmt.Sprintf(
					"reading ingest job %s: HTTP 403 (the job poll requires tenant_admin role)", jobID)),
				jsonOut,
			)
		case http.StatusNotFound:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"ingest job %s is no longer tracked by the backplane (pod restart or registry eviction); "+
						"the pipeline may have completed or died with the pod — check `meho connector list` "+
						"for the connector before re-running ingest", jobID)),
				jsonOut,
			)
		default:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"reading ingest job %s: HTTP %d: %s", jobID, he.statusCode, he.Error())),
				jsonOut,
			)
		}
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("wait for ingest job %s cancelled: %v", jobID, err)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("reading ingest job %s on %s: %v", jobID, backplaneURL, err)),
		jsonOut,
	)
}

// printIngestJobSnapshot renders a still-running job for human eyes:
// the identity (job_id + the originator's request descriptors echoed
// back so the operator doesn't have to correlate against their own
// state) and the lifecycle (status + started_at). Terminal jobs never
// reach here — they route through renderIngestTerminal.
func printIngestJobSnapshot(w io.Writer, st *api.IngestJobStatusResponse) {
	fmt.Fprintf(w, "ingest job %s — status=%s\n", st.JobId, st.Status)
	if descriptor := ingestJobDescriptor(st); descriptor != "" {
		fmt.Fprintf(w, "  request: %s\n", descriptor)
	}
	if specs := st.SpecUris; specs != nil && len(*specs) > 0 {
		for _, uri := range *specs {
			fmt.Fprintf(w, "  spec: %s\n", uri)
		}
	}
	// started_at is epoch seconds (float, sub-second precision). Render
	// it as a UTC timestamp the operator can read; the raw value stays
	// available via --json.
	if st.StartedAt > 0 {
		started := time.Unix(int64(st.StartedAt), 0).UTC()
		fmt.Fprintf(w, "  started_at: %s\n", started.Format(time.RFC3339))
	}
	fmt.Fprintf(w,
		"\nThe pipeline is still running server-side; re-running ingest would start a second job.\n"+
			"Re-run this command (add --wait to block until it completes), or watch\n"+
			"`meho connector list`, until the connector lands in review_status=staged.\n")
}

// ingestJobDescriptor renders the originator's request descriptors
// (catalog mode's `<product>/<version>` entry, or the explicit
// `<product>/<version>/<impl_id>` quadruple) into a one-line echo. The
// descriptors are optional pointers on the wire (a job read before the
// backplane populated them, or a shape this CLI predates, leaves them
// nil); an empty return means "nothing to echo" and the caller drops
// the line.
func ingestJobDescriptor(st *api.IngestJobStatusResponse) string {
	if st.CatalogEntry != nil && *st.CatalogEntry != "" {
		return "catalog " + *st.CatalogEntry
	}
	product := derefOr(st.Product, "?")
	version := derefOr(st.Version, "?")
	implID := derefOr(st.ImplId, "?")
	if product == "?" && version == "?" && implID == "?" {
		return ""
	}
	return fmt.Sprintf("%s/%s/%s", product, version, implID)
}

// derefOr returns *p when p is non-nil and non-empty, else fallback.
func derefOr(p *string, fallback string) string {
	if p != nil && *p != "" {
		return *p
	}
	return fallback
}
