// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// defaultIngestPollInterval is the steady-state delay between
// GET /api/v1/connectors/ingest/jobs/{job_id} polls while waiting
// for an async ingest job to reach a terminal status. The job-status
// read is an in-memory registry lookup backplane-side, so a 2s
// cadence is cheap; real vendor specs spend ~30s+ in the register +
// LLM-grouping phases, so anything much faster only burns requests.
const defaultIngestPollInterval = 2 * time.Second

// newIngestCmd returns the `meho connector ingest` command.
//
// CLI shape:
//
//	meho connector ingest \
//	  --product <p> --version <v> --impl <i> \
//	  --spec <uri> [--spec <uri> ...] \
//	  [--dry-run] [--no-wait] [--json] [--backplane <url>]
//
// The verb hits POST /api/v1/connectors/ingest with an IngestRequest
// body; the backplane runs T1 parser → T2 register_ingested → T3
// LLM grouping. Since #1303 the route defaults to the async shape:
// it fires the pipeline off the request thread and answers
// 202 Accepted + an IngestJobHandle. The CLI polls the handle to a
// terminal status by default (`--no-wait` exits 0 with the handle
// instead); a legacy 200 + IngestResponse (sync backplane, or
// --dry-run which always runs inline) renders directly. tenant_admin
// role required (HTTP 403 → exit 5).
func newIngestCmd() *cobra.Command {
	var (
		product           string
		versionFlag       string
		implID            string
		specs             []string
		compatible        []string
		catalog           string
		tenantID          string
		authScheme        string
		authSecretFields  []string
		dryRun            bool
		noWait            bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "ingest",
		Short: "Ingest one or more vendor specs into a new connector (staged state)",
		Long: "ingest parses each spec, registers the operations into the\n" +
			"endpoint_descriptor table under one connector_id, and runs the\n" +
			"LLM-summarised grouping pass. The newly-ingested connector lands\n" +
			"in review_status=staged — operations are NOT dispatchable until\n" +
			"an operator runs `meho connector review <id>` + `meho connector\n" +
			"enable <id>`.\n\n" +
			"Two mutually-exclusive modes:\n\n" +
			"  Catalog mode: --catalog <product>/<version> resolves the curated\n" +
			"  catalog entry (see `meho connector catalog list`) and ingests its\n" +
			"  recommended triple + upstream spec URL(s). Typed-connector and\n" +
			"  fqdn-templated entries are refused with a hint.\n\n" +
			"  Manual mode: --product + --version + --impl + one-or-more --spec.\n" +
			"  --spec accepts three URI shapes:\n" +
			"    - https://example.com/spec.yaml           (fetched by the backplane; https only)\n" +
			"    - file:///abs/path/to/spec.yaml          (read + uploaded by the CLI)\n" +
			"    - docs:<product-version>/<spec.yaml>      (CLI-side shorthand against\n" +
			"      $CLAUDE_RDC_DOCS; read + uploaded by the CLI like file://)\n" +
			"  Repeat --spec to merge multiple specs under one connector_id\n" +
			"  (vSphere is the canonical case: vcenter.yaml + vi-json.yaml).\n\n" +
			"  When a vendor spec self-versions independently of the product\n" +
			"  line (e.g. a version-stable /api/v2 surface reports\n" +
			"  info.version=v2 while the connector label is 9.0), pass\n" +
			"  --spec-info-versions-compatible with a glob (2.x, 9.0.x) or a\n" +
			"  PEP 440 specifier set (>=2,<3) to declare the band; the backplane\n" +
			"  then accepts the spec under --version instead of rejecting the\n" +
			"  major mismatch.\n\n" +
			"v0.12+ backplanes run the pipeline off the request thread and\n" +
			"answer 202 Accepted + a job handle. The CLI polls the job to a\n" +
			"terminal status and renders the usual summary on success; pass\n" +
			"--no-wait to exit 0 with the handle (job_id + poll URL) as soon\n" +
			"as the backplane accepts the work. Either way the job keeps\n" +
			"running server-side — re-running ingest after a 202 starts a\n" +
			"SECOND job, so poll the handle instead of retrying.\n\n" +
			"--dry-run parses + plans without writing to the DB; useful for\n" +
			"validating a spec before committing. Dry runs always execute\n" +
			"synchronously (200 + inline result). Role: tenant_admin.\n\n" +
			"--tenant-id selects the write scope for the ingested rows and\n" +
			"combines with either mode. Omit it (the default) to ingest under\n" +
			"the built-in / global scope (tenant_id IS NULL, visible to every\n" +
			"tenant) — the request then leaves tenant_id unset, the\n" +
			"omit-equals-global semantics the REST and MCP surfaces share.\n" +
			"Pass your OWN tenant UUID for a tenant-curated ingest; the\n" +
			"backplane rejects any other tenant's UUID with HTTP 403.\n\n" +
			"--auth-scheme (manual mode) selects a named auth scheme from the\n" +
			"closed catalog so the connector is stamped DISPATCHABLE (a profiled\n" +
			"connector), still staged behind review/enable — never auto-enabled.\n" +
			"Without it, an arbitrary spec ingests a non-dispatchable shim. The\n" +
			"allowed values are: basic, static_header, session_login,\n" +
			"session_login_basic, session_login_token, oauth2_mint. There is no\n" +
			"free-form auth config (no login URL/template/token path) — selection\n" +
			"only. --auth-secret-field overrides the secret-field NAMES the scheme\n" +
			"reads at dispatch (never the values — those stay in the target's\n" +
			"secret_ref); omit for the per-scheme defaults. Both are mutually\n" +
			"exclusive with --catalog (a catalog row binds its own profile).",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runIngest(cmd, ingestOptions{
				Product:           product,
				Version:           versionFlag,
				ImplID:            implID,
				Specs:             specs,
				Compatible:        compatible,
				Catalog:           catalog,
				TenantID:          tenantID,
				AuthScheme:        authScheme,
				AuthSecretFields:  authSecretFields,
				DryRun:            dryRun,
				NoWait:            noWait,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&product, "product", "",
		"product name (e.g. vmware, kubernetes); manual mode (required with --version/--impl/--spec)")
	cmd.Flags().StringVar(&versionFlag, "version", "",
		"product version (e.g. 9.0, 1.x); manual mode")
	cmd.Flags().StringVar(&implID, "impl", "",
		"impl identifier (e.g. vmware-rest, k8s-go); manual mode")
	cmd.Flags().StringArrayVar(&specs, "spec", nil,
		"spec URI; repeat for multi-spec merge under one connector_id; manual mode")
	cmd.Flags().StringSliceVar(&compatible, "spec-info-versions-compatible", nil,
		"manual mode: declare that the spec's info.version is compatible with --version even when "+
			"they differ (e.g. a vendor /api/v2 surface self-versioning as info.version=v2 ingested "+
			"under --version 9.0). Each entry is a glob (2.x, 9.0.x) or a PEP 440 specifier set "+
			"(>=2,<3); repeatable or comma-separated. Without it, a spec/label major mismatch is "+
			"rejected; mutually exclusive with --catalog (the catalog row carries its own band)")
	cmd.Flags().StringVar(&catalog, "catalog", "",
		"catalog mode: ingest the curated entry for <product>/<version> (e.g. vmware/9.0); "+
			"mutually exclusive with --product/--version/--impl/--spec")
	cmd.Flags().StringVar(&tenantID, "tenant-id", "",
		"write scope for the ingested rows (works with both modes): omit for the built-in / "+
			"global scope (tenant_id left unset — visible to every tenant); pass your own "+
			"tenant UUID for a tenant-curated ingest (another tenant's UUID is rejected with HTTP 403)")
	cmd.Flags().StringVar(&authScheme, "auth-scheme", "",
		"manual mode: select a named auth scheme (closed catalog) so the connector is stamped "+
			"DISPATCHABLE (a profiled connector, still staged behind review) instead of a "+
			"non-dispatchable shim. One of: basic, static_header, session_login, session_login_basic, "+
			"session_login_token, oauth2_mint. Selection only — no free-form auth config. Mutually "+
			"exclusive with --catalog")
	cmd.Flags().StringArrayVar(&authSecretFields, "auth-secret-field", nil,
		"manual mode: override a secret-field NAME the --auth-scheme reads at dispatch (never the "+
			"value — that stays in the target's secret_ref); repeat for multiple. Omit for the "+
			"per-scheme defaults. Requires --auth-scheme")
	cmd.Flags().BoolVar(&dryRun, "dry-run", false,
		"parse and plan without writing to the DB; the response carries an IngestionResult with counts but no GroupingResult")
	cmd.Flags().BoolVar(&noWait, "no-wait", false,
		"on an async 202 answer, exit 0 with the job handle (job_id + poll URL) instead of polling the job to completion; "+
			"no effect when the backplane answers synchronously (HTTP 200)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type ingestOptions struct {
	Product           string
	Version           string
	ImplID            string
	Specs             []string
	Compatible        []string
	Catalog           string
	TenantID          string
	AuthScheme        string
	AuthSecretFields  []string
	DryRun            bool
	NoWait            bool
	JSONOut           bool
	BackplaneOverride string

	// pollInterval overrides defaultIngestPollInterval; zero means
	// "use the default". Unexported test seam so the async-poll
	// tests don't sleep wall-clock seconds per iteration.
	pollInterval time.Duration
}

func runIngest(cmd *cobra.Command, opts ingestOptions) error {
	if err := validateIngestMode(opts); err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}

	body, err := buildIngestRequest(opts)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}

	authed, err := newAuthedClient(cmd.Context(), backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}

	start, err := postIngest(cmd.Context(), authed, body)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	if start.job != nil {
		return runIngestAsync(cmd, authed, backplaneURL, start.job, opts)
	}
	if opts.JSONOut {
		return output.PrintJSON(cmd.OutOrStdout(), start.sync)
	}
	printIngestSummary(cmd.OutOrStdout(), opts, start.sync)
	return nil
}

// buildIngestRequest assembles the POST body for either of the two
// mutually-exclusive request shapes (catalog-driven or explicit
// quadruple). Catalog mode (G0.14-T9 / #1150) ships the
// "<product>/<version>" reference verbatim — the backplane resolves
// the entry against the packaged catalog so REST-native clients and
// the CLI share the resolution path. Manual mode resolves each spec
// URI locally to give the operator a fast hint on a typo'd scheme.
//
// The generated `api.IngestRequest` carries the
// catalog-vs-quadruple-vs-`dry_run` discriminator on the wire via
// pointer fields the JSON serialiser omits when nil. Setting only
// what the operator asked for keeps the wire shape narrow and lets
// the backend's mutual-exclusivity validator stay green in both
// modes; the existing tests pin both branches.
func buildIngestRequest(opts ingestOptions) (api.IngestRequest, error) {
	body := api.IngestRequest{}
	if opts.DryRun {
		dr := true
		body.DryRun = &dr
	}
	if opts.TenantID != "" {
		// Write scope (#2085): orthogonal to the catalog/manual split, so
		// it rides both shapes. An unset flag leaves the field nil — the
		// omit-equals-global semantics the REST/MCP surfaces document.
		// Parsing here (not backplane-side) turns a typo'd UUID into an
		// immediate local error instead of a request round-trip.
		parsed, perr := uuid.Parse(opts.TenantID)
		if perr != nil {
			return api.IngestRequest{}, fmt.Errorf(
				"--tenant-id %q is not a valid UUID: %v (omit the flag for a global-scope ingest)",
				opts.TenantID, perr)
		}
		body.TenantId = &parsed
	}
	if opts.Catalog != "" {
		catalog := opts.Catalog
		body.CatalogEntry = &catalog
		return body, nil
	}
	specs := make([]api.SpecSource, 0, len(opts.Specs))
	for _, raw := range opts.Specs {
		uri, content, uerr := resolveSpecURI(raw)
		if uerr != nil {
			return api.IngestRequest{}, uerr
		}
		src := api.SpecSource{Uri: uri}
		if content != "" {
			src.Content = &content
		}
		specs = append(specs, src)
	}
	product := opts.Product
	version := opts.Version
	implID := opts.ImplID
	body.Product = &product
	body.Version = &version
	body.ImplId = &implID
	body.Specs = &specs
	if len(opts.Compatible) > 0 {
		// Explicit-quadruple opt-in (T1 #1646): the operator declares a
		// spec-info-version compatibility band so the backplane's
		// spec-vs-label cross-check widens against it instead of rejecting
		// a self-versioning vendor spec. Catalog mode returns above, so
		// this only ever rides the manual shape — the backend validator
		// rejects the field alongside catalog_entry.
		compatible := append([]string(nil), opts.Compatible...)
		body.SpecInfoVersionsCompatible = &compatible
	}
	if opts.AuthScheme != "" {
		// Non-catalog on-ramp (#2289): selecting a named auth scheme stamps
		// a dispatchable profiled connector (still review-gated) instead of a
		// bare shim. Catalog mode returns above, so this only rides the
		// manual shape. The closed-set / reserved-scheme rejection is the
		// backend's (a 422 naming the allowed members) — the CLI forwards the
		// operator's selection verbatim as the typed enum.
		scheme := api.IngestRequestAuthScheme(opts.AuthScheme)
		body.AuthScheme = &scheme
	}
	if len(opts.AuthSecretFields) > 0 {
		// NAMES only — the credential values are resolved from the target's
		// secret_ref at dispatch, never carried in the request.
		fields := append([]string(nil), opts.AuthSecretFields...)
		body.AuthSecretFields = &fields
	}
	return body, nil
}

// validateIngestMode enforces the catalog/manual split: exactly one
// mode, and manual mode needs the full triple + at least one --spec.
// Replaces the per-flag MarkFlagRequired wiring (which can't express
// "required unless --catalog").
func validateIngestMode(opts ingestOptions) error {
	if opts.NoWait && opts.DryRun {
		// Dry runs always execute synchronously backplane-side (the
		// parse-only leg returns 200 + the inline plan; there is no
		// job to detach from), so a combined flag set signals a
		// misunderstanding worth correcting rather than ignoring.
		return errors.New(
			"--no-wait cannot be combined with --dry-run: dry runs always execute " +
				"synchronously (the backplane returns the parse plan inline)")
	}
	if len(opts.AuthSecretFields) > 0 && opts.AuthScheme == "" {
		// Naming the secret fields without selecting a scheme is a caller-side
		// bug — there is no extractor to read them (mirrors the backend 422).
		return errors.New(
			"--auth-secret-field requires --auth-scheme (the field names are read " +
				"by the selected scheme's extractor)")
	}
	manualSet := opts.Product != "" || opts.Version != "" || opts.ImplID != "" || len(opts.Specs) > 0
	if opts.Catalog != "" {
		if manualSet {
			return errors.New(
				"--catalog cannot be combined with --product/--version/--impl/--spec; " +
					"use catalog mode OR manual mode, not both")
		}
		if opts.AuthScheme != "" {
			return errors.New(
				"--auth-scheme cannot be combined with --catalog; a catalog row binds its " +
					"own profile (use --auth-scheme only with the manual --product/--version/--impl/--spec shape)")
		}
		return nil
	}
	if !manualSet {
		return errors.New(
			"specify a connector to ingest: --catalog <product>/<version>, " +
				"or manual mode (--product --version --impl --spec)")
	}
	var missing []string
	if opts.Product == "" {
		missing = append(missing, "--product")
	}
	if opts.Version == "" {
		missing = append(missing, "--version")
	}
	if opts.ImplID == "" {
		missing = append(missing, "--impl")
	}
	if len(opts.Specs) == 0 {
		missing = append(missing, "--spec")
	}
	if len(missing) > 0 {
		return fmt.Errorf("manual ingest requires %s (or use --catalog <product>/<version>)",
			strings.Join(missing, ", "))
	}
	return nil
}

// ingestStart carries the two mutually-exclusive success shapes of
// POST /api/v1/connectors/ingest. Exactly one field is non-nil:
//
//   - sync — HTTP 200, the legacy blocking IngestResponse
//     (async=false backplanes, and every --dry-run regardless of
//     backplane version).
//   - job — HTTP 202, the #1303 async default: the pipeline runs
//     off the request thread and the handle names the job to poll.
//
// Callers branch on `job != nil` rather than re-reading the status
// code so the "202 is a success, not an error" contract lives in
// one place (this is the envelope the pre-#1609 CLI rendered as a
// fatal unexpected_response — after the work had already started
// server-side, which is what made retrying double-ingest).
type ingestStart struct {
	sync *api.IngestResponse
	job  *api.IngestJobHandle
}

// postIngest drives the typed-client ingest endpoint with a one-shot
// 401-retry. The route declares both success shapes (200 sync legacy,
// 202 async job handle) so JSON200 / JSON202 carry the typed
// envelopes; any other status surfaces as *httpResponseError for the
// caller to route through renderHTTPStatus.
func postIngest(ctx context.Context, authed *api.AuthedClient, body api.IngestRequest) (*ingestStart, error) {
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.IngestEndpointApiV1ConnectorsIngestPostResponse, error) {
			return authed.IngestEndpointApiV1ConnectorsIngestPostWithResponse(
				ctx,
				&api.IngestEndpointApiV1ConnectorsIngestPostParams{},
				body,
			)
		},
		func(r *api.IngestEndpointApiV1ConnectorsIngestPostResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return nil, err
	}
	switch resp.StatusCode() {
	case http.StatusOK:
		if resp.JSON200 == nil {
			return nil, fmt.Errorf("backplane returned 200 OK but no JSON body decoded against IngestResponse")
		}
		return &ingestStart{sync: resp.JSON200}, nil
	case http.StatusAccepted:
		if resp.JSON202 == nil {
			return nil, fmt.Errorf("backplane returned 202 Accepted but no JSON body decoded against IngestJobHandle")
		}
		return &ingestStart{job: resp.JSON202}, nil
	default:
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
}

// runIngestAsync finishes an ingest the backplane accepted with
// 202 + an IngestJobHandle. Two modes:
//
//   - --no-wait: render the handle (human key/value or the raw
//     IngestJobHandle JSON) and exit 0 — the job keeps running
//     server-side and the operator polls poll_url themselves.
//   - default: poll GET /api/v1/connectors/ingest/jobs/{job_id}
//     until the job leaves "running", then render exactly what the
//     sync path would have rendered (the assembled IngestResponse) —
//     so scripts parsing `--json` see one stable success shape
//     regardless of whether the backplane ran the pipeline inline
//     or off-thread. A failed job renders the job's error_class +
//     capped error message as unexpected_response (exit 4), the
//     same family the sync path's pipeline failures land in. A
//     degraded job (the pipeline ran but persisted nothing
//     dispatchable — claude-rdc-hetzner-dc#1136) renders the same
//     way so a non-dispatchable ingest is never mistaken for success.
//
// The progress notice goes to stderr in both output modes: stdout
// stays reserved for the final result (Goal #11 §5 output
// discipline — `--json` consumers must see a single JSON document).
func runIngestAsync(
	cmd *cobra.Command,
	authed *api.AuthedClient,
	backplaneURL string,
	handle *api.IngestJobHandle,
	opts ingestOptions,
) error {
	if opts.NoWait {
		if opts.JSONOut {
			return output.PrintJSON(cmd.OutOrStdout(), handle)
		}
		printIngestJobHandle(cmd.OutOrStdout(), handle)
		return nil
	}
	interval := opts.pollInterval
	if interval <= 0 {
		interval = defaultIngestPollInterval
	}
	fmt.Fprintf(cmd.ErrOrStderr(),
		"ingest accepted (HTTP 202) — job_id=%s; polling %s every %s until the job completes\n",
		handle.JobId, handle.PollUrl, interval)
	st, err := pollIngestJob(cmd.Context(), authed, handle.JobId, interval)
	if err != nil {
		return renderIngestWaitError(cmd, backplaneURL, handle, err, opts.JSONOut)
	}
	return renderIngestTerminal(cmd, st, opts.DryRun, opts.JSONOut)
}

// renderIngestTerminal renders a *terminal* IngestJobStatusResponse
// (the job has already left "running") to the exact shapes the
// async-ingest waiting path and the `ingest-status` verb both emit —
// this is the single lifecycle switch the task #1621 acceptance
// criterion pins ("one lifecycle switch, not copy-pasted"). Callers
// are responsible for never handing it a "running" status; that's a
// snapshot, not a terminal render, and lives in the verb that reads
// it (`ingest-status` without --wait).
//
//   - succeeded → the assembled IngestResponse (the same summary /
//     --json document the sync 200 path emits), exit 0.
//   - failed → error_class + capped error as unexpected_response
//     (exit 4), the family the sync path's pipeline failures land in.
//   - degraded → same exit-4 error shape, but the full job document
//     still rides out on stdout under --json for diagnosis (#1647):
//     a degraded job is a terminal failure, and a --json consumer
//     reading it as exit 0 is the false-success this guards against.
//   - any other status → loud unexpected_response (exit 4); never a
//     silent success.
//
// dryRun feeds printIngestSummary's heading (a polled job is never a
// dry run — dry runs execute synchronously — but the parameter keeps
// the summary call identical to the sync path's).
func renderIngestTerminal(
	cmd *cobra.Command,
	st *api.IngestJobStatusResponse,
	dryRun bool,
	jsonOut bool,
) error {
	switch st.Status {
	case api.IngestJobStatusResponseStatusSucceeded:
		if st.Ingestion == nil {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"ingest job %s reports succeeded but carries no ingestion result", st.JobId)),
				jsonOut,
			)
		}
		result := &api.IngestResponse{Ingestion: *st.Ingestion, Grouping: st.Grouping}
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), result)
		}
		printIngestSummary(cmd.OutOrStdout(), ingestOptions{DryRun: dryRun}, result)
		return nil
	case api.IngestJobStatusResponseStatusFailed:
		errClass := "IngestJobFailed"
		if st.ErrorClass != nil && *st.ErrorClass != "" {
			errClass = *st.ErrorClass
		}
		detail := "(no error detail recorded)"
		if st.Error != nil && *st.Error != "" {
			detail = *st.Error
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"ingest job %s failed: %s: %s", st.JobId, errClass, detail)),
			jsonOut,
		)
	case api.IngestJobStatusResponseStatusDegraded:
		// The pipeline ran to completion but left the connector
		// non-dispatchable (claude-rdc-hetzner-dc#1136): a bare
		// "succeeded" here was the false-success the server now refuses
		// to report. Surface it as an error (non-zero exit) carrying the
		// structured error_class + detail so the operator knows the
		// catalog row will read "registered, 0 ops" and what to do. The
		// ingestion counts ride along in --json output for diagnosis.
		errClass := "ingested_not_dispatchable"
		if st.ErrorClass != nil && *st.ErrorClass != "" {
			errClass = *st.ErrorClass
		}
		detail := "(no error detail recorded)"
		if st.Error != nil && *st.Error != "" {
			detail = *st.Error
		}
		if jsonOut {
			// Emit the full job document (counts + error_class) to stdout
			// for diagnosis, then still exit non-zero: a degraded job is a
			// terminal failure (#1647), and a --json consumer that read this
			// as exit 0 is the exact false-success this task closes, moved
			// to the CLI tier. jsonOut=false on RenderError keeps stdout the
			// clean JSON document (the human one-liner goes to stderr) while
			// the returned silentError carries ExitUnexpected.
			if err := output.PrintJSON(cmd.OutOrStdout(), st); err != nil {
				return err
			}
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"ingest job %s degraded: %s: %s", st.JobId, errClass, detail)),
				false,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"ingest job %s degraded: %s: %s", st.JobId, errClass, detail)),
			jsonOut,
		)
	default:
		// Forward-compat: a status outside the documented
		// running/succeeded/failed lifecycle fails loudly instead of
		// being treated as success (or spinning forever in the poll
		// loop, which only continues on "running").
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"ingest job %s returned undocumented status %q", st.JobId, st.Status)),
			jsonOut,
		)
	}
}

// pollIngestJob reads the job row until it leaves "running", with
// the same one-shot 401-refresh contract every connector verb uses
// (a token can expire mid-wait on a long LLM-grouping pass; the
// refresh keeps the poll alive without operator action). Errors are
// returned raw — renderIngestWaitError owns the job-aware
// classification, because every poll-phase failure message must
// carry the "job keeps running server-side, don't re-run ingest"
// guidance.
func pollIngestJob(
	ctx context.Context,
	authed *api.AuthedClient,
	jobID uuid.UUID,
	interval time.Duration,
) (*api.IngestJobStatusResponse, error) {
	for {
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
			return nil, fmt.Errorf("backplane returned 200 OK on the job poll but no JSON body decoded against IngestJobStatusResponse")
		}
		if resp.JSON200.Status != api.IngestJobStatusResponseStatusRunning {
			return resp.JSON200, nil
		}
		if serr := sleepCtx(ctx, interval); serr != nil {
			return nil, serr
		}
	}
}

// renderIngestWaitError classifies a poll-phase failure. Unlike the
// pre-submit failure paths (renderHTTPStatus / renderRequestError),
// every message here must orient the operator around one fact: the
// ingest job already started server-side, so the safe reaction to a
// broken wait is to re-check the job (poll_url or `meho connector
// list`), never to re-run `meho connector ingest` — a re-run starts
// a second job (the double-ingest failure mode this verb's 202
// handling exists to prevent).
func renderIngestWaitError(
	cmd *cobra.Command,
	backplaneURL string,
	handle *api.IngestJobHandle,
	err error,
	jsonOut bool,
) error {
	recheck := fmt.Sprintf(
		"the job keeps running server-side — re-check it with `meho connector ingest-status %s` "+
			"(or `meho connector list`) before re-running ingest, a re-run would start a second job", handle.JobId)
	var he *httpResponseError
	if errors.As(err, &he) {
		switch he.statusCode {
		case http.StatusUnauthorized:
			return output.RenderError(cmd.ErrOrStderr(),
				output.AuthExpired(fmt.Sprintf(
					"backplane rejected the stored token while polling ingest job %s; run `meho login %s`; %s",
					handle.JobId, backplaneURL, recheck)),
				jsonOut,
			)
		case http.StatusForbidden:
			return output.RenderError(cmd.ErrOrStderr(),
				output.InsufficientRole(fmt.Sprintf(
					"polling ingest job %s: HTTP 403 (the job poll requires tenant_admin role); %s",
					handle.JobId, recheck)),
				jsonOut,
			)
		case http.StatusNotFound:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"ingest job %s is no longer tracked by the backplane (pod restart or registry eviction); "+
						"the pipeline may have completed or died with the pod — check `meho connector list` "+
						"for the connector before re-running ingest (`meho connector ingest-status %s` reports the "+
						"same lost-job state)", handle.JobId, handle.JobId)),
				jsonOut,
			)
		default:
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"polling ingest job %s: HTTP %d: %s; %s",
					handle.JobId, he.statusCode, strings.TrimSpace(string(he.body)), recheck)),
				jsonOut,
			)
		}
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"wait for ingest job %s cancelled: %v; %s", handle.JobId, err, recheck)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf(
			"polling ingest job %s on %s: %v; %s", handle.JobId, backplaneURL, err, recheck)),
		jsonOut,
	)
}

// printIngestJobHandle renders the --no-wait handle for human eyes:
// the job_id first (the thing the operator copies), the poll URL,
// and the workflow reminder that makes the detached mode safe —
// the next action is polling, not re-running ingest.
func printIngestJobHandle(w io.Writer, handle *api.IngestJobHandle) {
	fmt.Fprintf(w, "ingest accepted — job_id=%s (status=%s)\n", handle.JobId, handle.Status)
	fmt.Fprintf(w, "  poll: GET %s\n", handle.PollUrl)
	fmt.Fprintf(w,
		"\nThe pipeline keeps running server-side; re-running ingest would start a second job.\n"+
			"Check the job with `meho connector ingest-status %s` (add --wait to block until it\n"+
			"completes), or watch `meho connector list`, until the connector lands in\n"+
			"review_status=staged, then:\n"+
			"  meho connector review <connector_id>\n"+
			"  meho connector enable <connector_id> --confirm\n", handle.JobId)
}

// sleepCtx waits d or until ctx is cancelled, whichever comes first.
// Returns ctx.Err() on cancellation, nil on timer fire. Same shape
// as the status --watch retry loop's helper (internal/cmd/
// status_watch.go) — duplicated because cmd/connector can't import
// the root cmd package without a cycle (cmd/root.go grafts this
// package onto the tree).
func sleepCtx(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// printIngestSummary renders an IngestResponse for human eyes. The
// shape matches what an operator running `meho connector ingest`
// interactively expects to read: connector_id first (so they can
// copy it into the subsequent `review` / `enable` commands), then
// the bulk-upsert counts, then the LLM grouping outcome (or "dry
// run — skipped" on dry-run).
//
// The canonical IngestionResultModel ships only the aggregate
// inserted/updated/skipped counts plus the two boolean flags
// (connector_registered, operations_grouped). The per-spec
// breakdown and the embeddings split that the original PR-body
// contract carried are not in the wire shape — operators see the
// aggregate via this rollup and the per-spec story via the audit log.
//
// The `<product>/<version>/<impl_id>` heading is derived from the
// response's `connector_id` rather than `opts.Product/Version/ImplID`
// because catalog mode (G0.14-T9 / #1150) leaves those opts empty —
// the backplane resolves the catalog entry server-side and returns
// the resolved triple via `connector_id`. Deriving from the response
// keeps the heading correct in both modes and matches the pre-#1150
// operator-visible output.
func printIngestSummary(w io.Writer, opts ingestOptions, r *api.IngestResponse) {
	totalOps := r.Ingestion.InsertedCount + r.Ingestion.UpdatedCount + r.Ingestion.SkippedCount
	heading := ingestSummaryHeading(r.Ingestion.ConnectorId)
	if opts.DryRun {
		fmt.Fprintf(w, "ingest %s — DRY RUN (no DB writes)\n", heading)
	} else {
		fmt.Fprintf(w, "ingest %s — connector_id=%s\n",
			heading, r.Ingestion.ConnectorId,
		)
	}
	fmt.Fprintf(w, "  operations: %d total (%d inserted / %d updated / %d skipped)\n",
		totalOps,
		r.Ingestion.InsertedCount,
		r.Ingestion.UpdatedCount,
		r.Ingestion.SkippedCount,
	)
	if !opts.DryRun {
		fmt.Fprintf(w, "  connector_registered: %t (first ingest of this triple flips it to true)\n",
			r.Ingestion.ConnectorRegistered,
		)
		fmt.Fprintf(w, "  operations_grouped: %t\n", r.Ingestion.OperationsGrouped)
	}
	if r.Grouping != nil {
		fmt.Fprintf(w, "  grouping: %d groups, %d ops assigned, %d unassigned",
			r.Grouping.GroupsCreated,
			r.Grouping.OperationsAssigned,
			r.Grouping.OperationsUnassigned,
		)
		if r.Grouping.LlmCallCount > 0 {
			fmt.Fprintf(w, " (%d LLM call(s), %.0fms)",
				r.Grouping.LlmCallCount, r.Grouping.LlmDurationMs,
			)
		}
		fmt.Fprintln(w)
	} else if !opts.DryRun {
		fmt.Fprintln(w, "  grouping: skipped (backplane returned no grouping result)")
	}
	if !opts.DryRun {
		fmt.Fprintf(w,
			"\nConnector is in review_status=staged. Next:\n"+
				"  meho connector review %s\n"+
				"  meho connector enable %s --confirm\n",
			r.Ingestion.ConnectorId, r.Ingestion.ConnectorId,
		)
	}
}

// ingestSummaryHeading derives the `<product>/<version>/<impl_id>`
// heading from a response connector_id. Both ingest modes route
// through this helper so the operator-visible output is identical
// to v0.6.0 regardless of which request shape (catalog or explicit
// quadruple) the CLI used. The backend resolves the catalog entry
// server-side, so deriving from the response is what makes the
// catalog-mode heading carry the resolved triple instead of empty
// `//` placeholders.
//
// Mirrors `parse_connector_id` in
// `backend/src/meho_backplane/operations/ingest/parser.py` — the
// operator-facing identifier is `<impl_id>-<version>` where
// `version` starts with a digit; `product` is the first
// dash-segment of `impl_id`. If the response carries a
// non-conforming connector_id (shouldn't happen in practice — the
// backend builds it from a validated triple) we fall back to
// echoing the connector_id verbatim so the operator still sees
// something useful instead of bare slashes.
func ingestSummaryHeading(connectorID string) string {
	for i, ch := range connectorID {
		if ch != '-' || i+1 >= len(connectorID) {
			continue
		}
		next := connectorID[i+1]
		if next < '0' || next > '9' {
			continue
		}
		implID := connectorID[:i]
		version := connectorID[i+1:]
		if implID == "" {
			return connectorID
		}
		product := implID
		if first := strings.IndexByte(implID, '-'); first != -1 {
			product = implID[:first]
		}
		return fmt.Sprintf("%s/%s/%s", product, version, implID)
	}
	return connectorID
}
