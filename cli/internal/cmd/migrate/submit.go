// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"charm.land/huh/v2"
	"charm.land/huh/v2/spinner"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/migrate"
	"github.com/evoila/meho/cli/internal/output"
)

// submitResult tallies the outcome of a submitPlans call.
type submitResult struct {
	Migrated int
	Skipped  int
	Errored  int
	Retried  int
}

func (r submitResult) String() string {
	return fmt.Sprintf("Migrated: %d, Skipped: %d, Errored: %d (retried %d)",
		r.Migrated, r.Skipped, r.Errored, r.Retried)
}

const maxAutoRetry = 3

// runSpinnerFn is the spinner seam: tests replace it with accessible mode
// to avoid opening /dev/tty in a headless environment.
var runSpinnerFn = func(sp *spinner.Spinner) error { return sp.Run() }

// doSubmit orchestrates per-entry submission. The interactive error prompt runs
// outside the spinner to avoid concurrent tea programs sharing the same TTY.
func doSubmit(cmd *cobra.Command, backplaneOverride string, plans []migrate.SubmitPlan) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), false)
	}

	nonInteractive, _ := cmd.Flags().GetBool("non-interactive")
	var res submitResult

	for i := range plans {
		p := plans[i]
		if err := postWithRetry(cmd, backplaneURL, p, nonInteractive, &res); err != nil {
			if errors.Is(err, errAborted) {
				break
			}
			fmt.Fprintln(cmd.OutOrStdout(), res.String())
			// Route the permanent error through renderSubmitError so the
			// exit code reflects the failure class (auth_expired=2 for
			// expired/missing/refresh-rejected tokens, unexpected=4 for
			// non-2xx and parse failures, unreachable=3 for transport
			// errors). Without this, every permanent error would exit 1 —
			// same code as a generic CLI error — and `meho migrate memory`
			// in a cron job would be indistinguishable from an unrelated
			// CLI flag bug.
			return renderSubmitError(cmd, backplaneURL, err)
		}
	}

	fmt.Fprintln(cmd.OutOrStdout(), res.String())
	if res.Errored > 0 {
		noun := "entries"
		if res.Errored == 1 {
			noun = "entry"
		}
		return fmt.Errorf("meho migrate memory: %d %s failed to migrate", res.Errored, noun)
	}
	return nil
}

var errAborted = errors.New("migration aborted by user")

// transientStatusError carries a non-2xx server response back from
// postOne so postWithRetry's transient-vs-permanent switch can act on
// the status code. Lifts the status off the typed response envelope
// after the verb's own JSON201 nil-guard runs; we need the numeric
// code (not a rendered output.StructuredError) for the retry-vs-abort
// decision.
type transientStatusError struct {
	StatusCode int
	Body       string
}

func (e *transientStatusError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// postWithRetry submits one plan entry. In non-interactive mode it auto-retries
// on transient errors (up to maxAutoRetry attempts). In interactive mode, each
// attempt runs inside its own spinner; the Retry/Skip/Abort prompt is presented
// outside the spinner so no two tea programs run concurrently.
func postWithRetry(
	cmd *cobra.Command,
	backplaneURL string,
	plan migrate.SubmitPlan,
	nonInteractive bool,
	res *submitResult,
) error {
	attempt := 0
	for {
		attempt++
		var postErr error
		sp := spinner.New().
			Title(fmt.Sprintf("Migrating %q…", plan.Slug)).
			ActionWithErr(func(ctx context.Context) error {
				postErr = postOne(ctx, backplaneURL, plan)
				return nil
			})
		if err := runSpinnerFn(sp); err != nil {
			return err
		}

		if postErr == nil {
			res.Migrated++
			return nil
		}

		if !isTransient(postErr) {
			res.Errored++
			// Bubble the raw error up — doSubmit routes it through
			// renderSubmitError for proper exit-code classification.
			// fmt.Errorf with %w preserves errors.As against the
			// status / typed-parse error categories below.
			return fmt.Errorf("entry %s: %w", plan.Slug, postErr)
		}

		// Transient error: auto-retry in non-interactive mode.
		if nonInteractive {
			if attempt <= maxAutoRetry {
				res.Retried++
				continue
			}
			res.Errored++
			fmt.Fprintf(cmd.ErrOrStderr(),
				"meho migrate memory: skipping %s after %d retries: %v\n",
				plan.Slug, attempt, postErr)
			return nil
		}

		// Interactive mode: prompt runs outside the spinner (no concurrent tea programs).
		var choice string
		prompt := huh.NewSelect[string]().
			Title(fmt.Sprintf("Error migrating %q: %v", plan.Slug, postErr)).
			Options(
				huh.NewOption("Retry", "retry"),
				huh.NewOption("Skip", "skip"),
				huh.NewOption("Abort", "abort"),
			).
			Value(&choice)
		if runErr := huh.NewForm(huh.NewGroup(prompt)).Run(); runErr != nil {
			return errAborted
		}
		switch choice {
		case "retry":
			res.Retried++
			continue
		case "skip":
			res.Skipped++
			return nil
		default:
			return errAborted
		}
	}
}

// postOne drives one POST /api/v1/memory through the generated typed
// client. Errors:
//
//   - transport / parse failures bubble up verbatim (renderSubmitError
//     classifies them into output.Unreachable or output.Unexpected).
//   - non-2xx responses return a *transientStatusError carrying the
//     status code + body so postWithRetry can decide retry-vs-abort and
//     renderSubmitError can map the code to a category.
//   - a 201 without a decoded JSON201 payload (Content-Type drift
//     between backplane and parser) surfaces as *transientStatusError
//     too, marked unexpected — mirrors the convention in
//     cli/internal/cmd/status.go:142 and the kb iter-2 nil-guard
//     pattern.
//
// The buildRememberBody seam keeps the wire shape isolated so a
// regenerated client (oapi-codegen renames a field, the FastAPI route
// adds a required field) surfaces as a compile error here rather than
// at every call site.
func postOne(ctx context.Context, backplaneURL string, plan migrate.SubmitPlan) error {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return err
	}
	body := buildRememberBody(plan)
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RememberApiV1MemoryPostResponse, error) {
			return authed.RememberApiV1MemoryPostWithResponse(
				ctx,
				&api.RememberApiV1MemoryPostParams{},
				body,
			)
		},
		func(r *api.RememberApiV1MemoryPostResponse) int { return r.StatusCode() },
	)
	if err != nil {
		return err
	}
	if resp.StatusCode() != http.StatusCreated {
		return &transientStatusError{
			StatusCode: resp.StatusCode(),
			Body:       strings.TrimSpace(string(resp.Body)),
		}
	}
	// Generated ParseRememberApiV1MemoryPostResponse only populates
	// JSON201 when the response has Content-Type containing "json" AND
	// status 201. A backplane / proxy that returns 201 with a missing
	// or mistyped content-type leaves JSON201 nil; without this guard
	// res.Migrated would tick up but the operator never gets a chance
	// to notice the contract drift. Mirrors the convention in
	// cli/internal/cmd/status.go:142 and the kb iter-2 nil-guard
	// pattern.
	if resp.JSON201 == nil {
		return &transientStatusError{
			StatusCode: resp.StatusCode(),
			Body:       fmt.Sprintf("HTTP 201 without a memory entry payload (body=%dB)", len(resp.Body)),
		}
	}
	return nil
}

// buildRememberBody maps a SubmitPlan onto the generated POST body.
// The scope MUST round-trip through api.MemoryScope so any backend
// rename of a scope constant surfaces as a compile error here rather
// than as a 422 on the wire. The metadata payload ("tags=[]") matches
// the legacy hand-typed body shape so cron jobs running this CLI on
// upgrade see byte-identical wire payloads — modulo the source_id
// field, which the legacy hand-typed body sent but the backend's
// frozen extra="forbid" RememberBody schema rejects (the server
// computes source_id itself from `(scope, user_sub, target_name,
// slug)` in `memory/_internal.py::encode_source_id`). Tests masked
// the latent 422 because the httptest mock accepted any JSON; the
// migration to the typed client both modernises the transport and
// closes the #1069-class drift this Initiative #1118 targets.
func buildRememberBody(plan migrate.SubmitPlan) api.RememberBody {
	scope := api.MemoryScope(plan.Scope)
	slug := plan.Slug
	md := map[string]interface{}{"tags": []string{}}
	return api.RememberBody{
		Scope:    scope,
		Slug:     &slug,
		Body:     plan.Body,
		Metadata: &md,
	}
}

// isTransient returns true for errors that are worth retrying: network
// timeouts (any non-status error returned by postOne) and 500/502/503/
// 504 from the backplane. 201-without-body (transientStatusError with
// status 201) is NOT transient — the server confirmed the row exists,
// the contract failure is on the response shape.
//
// Credential-state failures returned by newAuthedClient
// (errMissingAccessToken, api.IsTokenNotFound, api.IsNoRefreshToken)
// short-circuit BEFORE any HTTP call leaves the process — retrying
// cannot resolve them. The pre-T11 code resolved the auth token outside
// the retry loop, so these errors never reached isTransient; the
// migration moved the call site into the loop, which would otherwise
// classify them as transient and burn three retries (non-interactive)
// or three Retry/Skip/Abort prompts (interactive) before
// renderSubmitError finally rendered the correct auth_expired hint.
// Short-circuit here so postWithRetry sees a single non-transient
// failure and routes straight to renderSubmitError.
func isTransient(err error) bool {
	var tse *transientStatusError
	if errors.As(err, &tse) {
		switch tse.StatusCode {
		case http.StatusInternalServerError,
			http.StatusBadGateway,
			http.StatusServiceUnavailable,
			http.StatusGatewayTimeout:
			return true
		}
		return false
	}
	// Credential failures from newAuthedClient surface before the HTTP
	// call; no amount of retrying gets the operator a token. Match the
	// three sentinels renderSubmitError already maps to auth_expired so
	// the retry-vs-permanent decision and the exit-code classification
	// agree on what "credential failure" means.
	if errors.Is(err, errMissingAccessToken) ||
		api.IsTokenNotFound(err) ||
		api.IsNoRefreshToken(err) {
		return false
	}
	// Transport / parse errors (connection refused, timeout, MaxBytesError,
	// JSON unmarshal) are transient.
	return true
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure renderSubmitError maps to auth_expired
// with a `meho login` hint. Mirrors the agent-principal / kb / agent
// package shapes so an operator sees the same hint across every
// verb tree.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Centralised
// so the typed-call path goes through the same "stored-token-loaded +
// non-empty bearer" gate; the caller forwards any returned error to
// renderSubmitError for category mapping.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors the
// behaviour api.AuthedClient.GetHealth implements for /api/v1/health,
// generalised so the migrate verb runs the same transparent-retry
// contract as the agent-principal / kb / agent siblings.
//
// statusOf reads the StatusCode off the typed response envelope. A
// nil response counts as "no retry" — the transport already failed
// and the caller surfaces err directly.
func retryOn401[R any](
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*R, error),
	statusOf func(*R) int,
) (*R, error) {
	resp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if resp == nil || statusOf(resp) != http.StatusUnauthorized {
		return resp, nil
	}
	if rerr := authed.Refresh(ctx); rerr != nil {
		return resp, rerr
	}
	return call(ctx)
}

// renderSubmitError translates a postOne error into the right
// output.StructuredError category.
//
// Parse / cap failures route to output.Unexpected (exit 4 —
// unexpected_response) rather than output.Unreachable (exit 3 —
// network_unreachable). A JSON decode rejecting a malformed payload
// or a body-cap firing is a contract / shape failure on the server
// side, not a transport-down failure on the operator's side;
// surfacing it as "unreachable" would send operators chasing a
// network ghost. Mirrors the kb iter-2 classification.
func renderSubmitError(cmd *cobra.Command, backplaneURL string, err error) error {
	if errors.Is(err, errMissingAccessToken) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			false,
		)
	}
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			false,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			false,
		)
	}
	var tse *transientStatusError
	if errors.As(err, &tse) {
		return renderHTTPStatus(cmd, backplaneURL, tse.StatusCode, []byte(tse.Body))
	}
	// Transport-layer body-cap firing and JSON shape failures bubbling
	// out of the generated parsers are server-side contract failures,
	// not transport-down failures — surface them as unexpected_response
	// (exit 4) with the backplane URL so the operator sees the origin
	// without chasing a network ghost. Mirrors the kb iter-2
	// classification.
	var maxBytesErr *http.MaxBytesError
	var syntaxErr *json.SyntaxError
	var unmarshalErr *json.UnmarshalTypeError
	if errors.As(err, &maxBytesErr) ||
		errors.As(err, &syntaxErr) ||
		errors.As(err, &unmarshalErr) ||
		errors.Is(err, io.ErrUnexpectedEOF) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: %v", backplaneURL, err)),
			false,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		false,
	)
}

// renderHTTPStatus classifies a non-2xx response (or 201 without a
// decoded payload, treated as an unexpected contract failure) into
// the right StructuredError category. Preserves the pre-migration
// mapping:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role with the backend's detail string.
//   - Other non-2xx → unexpected with the raw body.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
) error {
	bodyStr := strings.TrimSpace(string(body))
	switch statusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected the stored token; run `meho login %s`",
				backplaneURL,
			)),
			false,
		)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)),
			false,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, statusCode, bodyStr)),
			false,
		)
	}
}

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail string `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body. Returns the raw body string if the body is not valid JSON or
// the `detail` field is missing / empty.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil && env.Detail != "" {
		return env.Detail
	}
	return body
}
