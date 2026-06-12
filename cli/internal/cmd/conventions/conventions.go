// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package conventions hosts the cobra commands under `meho conventions ...`
// for G7.1-T3 (#315) of Initiative #229 (tenant conventions). v0.2 ships
// six operator-facing verbs that wrap the T2 REST surface
// (`backend/src/meho_backplane/api/v1/conventions.py`) shipped by
// G7.1-T2 (#314):
//
//   - `meho conventions list [--kind K] [--json]` — paginated listing via
//     GET /api/v1/conventions. Role: operator.
//   - `meho conventions show <slug> [--json]` — single-convention fetch
//     via GET /api/v1/conventions/{slug}. Role: operator. Renders the
//     body as plain Markdown unless --json is set.
//   - `meho conventions create --slug S --kind K --title T --body @file
//     [--priority N] [--json]` — create via POST /api/v1/conventions.
//     Role: tenant_admin.
//   - `meho conventions edit <slug> [--title T] [--body @file]
//     [--priority N] [--json]` — partial PATCH via PATCH
//     /api/v1/conventions/{slug}. Role: tenant_admin. With no --title /
//     --body / --priority flags, opens $EDITOR on the current body and
//     submits the saved content as a PATCH (the operator surface for
//     conversational rule edits).
//   - `meho conventions delete <slug> [--confirm] [--json]` — delete via
//     DELETE /api/v1/conventions/{slug}. Role: tenant_admin.
//   - `meho conventions history <slug> [--limit N] [--json]` — history
//     trail via GET /api/v1/conventions/{slug}/history. Role: operator.
//     Renders unified diffs between consecutive entries by default.
//
// Each verb wraps one backplane route. Authentication piggybacks on the
// token meho login wrote — same pattern as `meho kb`, `meho agent`,
// `meho audit`. The backend writes the audit_log row keyed by op_id
// (`conventions.<verb>`); the CLI just calls the route.
//
// G0.12-T8 #1266 migrated this package off the sibling-verb pattern of
// hand-rolled HTTP + hand-typed copies of backend pydantic models.
// Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and
// the verbs call the typed client methods (`ListConventionsApiV1ConventionsGet`
// etc.) that own URL building, header injection, and request body
// encoding from the generated query-param / body types
// (`api.ConventionCreate`, `api.ConventionUpdate`,
// `api.ListConventionsApiV1ConventionsGetParams`). Consumer-side
// struct drift — the #1069 root cause Initiative #1118 targets —
// can't recur because we now consume `api.Convention`,
// `api.ConventionSummary`, `api.ConventionListResponse`,
// `api.ConventionHistoryEntry`, `api.BudgetStatus` directly.
//
// We deliberately bind to the lower-level non-`*WithResponse` methods
// (each returns `*http.Response`, not the typed `*<Op>Response`
// envelope) so the generated parser's HTTPValidationError-only 422
// unmarshal doesn't swallow the over-budget string-detail 422 the
// conventions surface emits (G7.1-T7 #1094). The shared `doRequest`
// helper reads the body once into a `rawResponse` (status + bytes)
// and the verbs unmarshal 2xx payloads themselves; non-2xx flows
// through `renderHTTPStatus` uniformly regardless of whether the body
// matches the OpenAPI spec's declared error shape.
package conventions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho conventions` parent command. Grafted onto
// the top-level meho tree by cmd/root.go alongside `meho kb`,
// `meho agent`, etc. The parent itself takes no args and prints its own
// help; every behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "conventions",
		Short: "Manage tenant conventions (list / show / create / edit / delete / history)",
		Long: "Manage tenant-scoped operational / workflow / reference " +
			"conventions wired by G7.1. A convention is a Markdown rule " +
			"the MEHO agent runtime auto-loads into the session preamble " +
			"(operational kind) or surfaces on demand (workflow / " +
			"reference). Write verbs (create / edit / delete) require " +
			"tenant_admin; read verbs (list / show / history) are " +
			"operator-level. Tenant scoping is enforced server-side via " +
			"the JWT — no surface accepts a tenant id, and cross-tenant " +
			"probes return 404 so existence is not leaked across tenant " +
			"boundaries.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newEditCmd())
	cmd.AddCommand(newDeleteCmd())
	cmd.AddCommand(newHistoryCmd())
	return cmd
}

// validKinds mirrors the backend ConventionKind enum. CLI-side
// validation gives the operator an immediate rejection rather than a
// remote 422.
var validKinds = map[string]bool{"operational": true, "workflow": true, "reference": true}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure renderRequestError maps to auth_expired
// with a `meho login` hint. Mirrors the agent-principal / approvals
// packages' shape so an operator sees the same hint across every verb
// tree.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Centralised
// so every verb's typed-call path goes through the same
// "stored-token-loaded + non-empty bearer" gate; the caller forwards
// any returned error to renderRequestError for category mapping.
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

// rawResponse is the (status, body) pair the verbs operate on after a
// successful HTTP round-trip. We bind to `*http.Response` rather than
// the generated `*WithResponse` envelope because the conventions
// surface emits two distinct 422 shapes (string detail for the
// over-budget gate; list detail for pydantic validation), only one of
// which matches the OpenAPI spec's HTTPValidationError. The generated
// parser surfaces a json.Unmarshal error on the unspec'd shape and
// drops the response on the floor — costing us the status + body
// we need to render the right error category. Reading the body
// ourselves bypasses that limitation while keeping every URL / query
// / body shape in lock-step with the generated client (the typed
// non-WithResponse methods own URL building, header injection, and
// request encoding).
type rawResponse struct {
	StatusCode int
	Body       []byte
}

// readAllBody drains rsp.Body up to the responseBodyCap, closing the
// body before returning. Splitting this out keeps each verb's call
// site to a single line and makes the cap auditable in one place.
func readAllBody(rsp *http.Response) ([]byte, error) {
	defer rsp.Body.Close() //nolint:errcheck
	raw, err := io.ReadAll(io.LimitReader(rsp.Body, responseBodyCap+1))
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf(
			"response body exceeds %d-byte cap; refusing to decode possibly-truncated JSON",
			responseBodyCap,
		)
	}
	return raw, nil
}

// responseBodyCap bounds the response body the CLI will read. A
// convention body is a Markdown rule (the operational kind is hard-
// capped at the preamble token budget, ~3 KB; workflow / reference can
// be larger but ~50 KB is realistic). 1 MiB is comfortable headroom
// and protects against an adversarial / misconfigured backplane sending
// an unbounded response.
const responseBodyCap int64 = 1 << 20

// doRequest invokes call once and, if the resulting response status
// is 401, runs a one-shot bearer refresh + re-issues call. Mirrors
// the behaviour `api.AuthedClient.GetHealth` implements for
// /api/v1/health, generalised so every conventions verb runs the same
// transparent-retry contract. Returns the drained (status, body) pair
// on the final response.
//
// The conventions package binds to the lower-level non-WithResponse
// client methods (each returns *http.Response, not the
// `*<Op>Response` envelope) so the generated parser's
// HTTPValidationError-only 422 unmarshal doesn't swallow the
// over-budget string-detail 422 the conventions surface emits.
// Reading the body via readAllBody lets renderHTTPStatus classify
// every non-2xx uniformly off the (status, body) pair, regardless of
// whether the body matches the OpenAPI spec's 422 shape.
func doRequest(
	ctx context.Context,
	authed *api.AuthedClient,
	call func(ctx context.Context) (*http.Response, error),
) (*rawResponse, error) {
	rsp, err := call(ctx)
	if err != nil {
		return nil, err
	}
	if rsp.StatusCode == http.StatusUnauthorized {
		// Drain + close before re-issuing so the underlying transport
		// can reuse the connection.
		_, _ = io.Copy(io.Discard, rsp.Body)
		rsp.Body.Close() //nolint:errcheck
		if rerr := authed.Refresh(ctx); rerr != nil {
			return nil, rerr
		}
		rsp, err = call(ctx)
		if err != nil {
			return nil, err
		}
	}
	raw, err := readAllBody(rsp)
	if err != nil {
		return nil, err
	}
	return &rawResponse{StatusCode: rsp.StatusCode, Body: raw}, nil
}

// renderRequestError translates a transport-layer request error into
// the right output.StructuredError category. Maps the conventions REST
// surface's pre-response failures: missing bearer, no-refresh-token,
// token-not-found, plus the generic transport-down case. Non-2xx
// status codes carried in a typed response envelope are classified by
// renderHTTPStatus instead.
//
//   - empty stored bearer → auth_expired.
//   - `meho login` not yet run → auth_expired with the file-store hint.
//   - 401 after a failed refresh (no refresh_token) → auth_expired.
//   - Pure transport errors → unreachable.
func renderRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if errors.Is(err, errMissingAccessToken) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored credentials for %s are incomplete; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. Mirrors the pre-migration `httpError`
// switch but acts on the (statusCode, body) pair lifted off the
// generated `*Response.HTTPResponse` + `Body` fields rather than a
// sentinel value.
//
// The mapping preserved across the migration:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role with the backend's detail string.
//   - 404 → unexpected with the backend's detail
//     (convention_not_found; cross-tenant probes land here per the
//     no-existence-leak posture).
//   - 409 → unexpected with the backend's detail
//     (convention_already_exists on create with a slug that already
//     exists in the same tenant).
//   - 422 → unexpected, prefixed with "invalid request: ". The
//     conventions surface emits two distinct 422s: pydantic
//     validation envelopes (`{"detail":[{...}]}`) on malformed
//     input, and the over-budget gate string detail when an
//     `operational` body exceeds DEFAULT_MAX_PREAMBLE_TOKENS.
//     decodeDetailString returns the string detail verbatim for
//     the budget gate; the validation envelope falls through to
//     the raw body. Both surface as unexpected so callers can branch
//     on exit code 4.
//   - Other non-2xx → unexpected with the raw body.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	bodyStr := strings.TrimSpace(string(body))
	switch statusCode {
	case http.StatusUnauthorized:
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"backplane rejected the stored token; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	case http.StatusForbidden:
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// `show / edit / delete / history` on an absent slug surface
		// here; the backend's `convention_not_found` covers both genuine
		// absence and cross-tenant probes (the conflation prevents
		// enumerating other tenants via status-code differential). For
		// `list` / `create` (no slug in the path) a 404 means the route
		// doesn't exist on this backplane — typically an older deploy
		// without T2.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		// `create` with a slug that already exists in the same tenant
		// surfaces here (`convention_already_exists`).
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", decodeDetailString(bodyStr))),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, statusCode, bodyStr)),
			jsonOut,
		)
	}
}

// detailEnvelope models FastAPI's HTTPException JSON shape. `detail`
// is a `json.RawMessage` so both shapes the conventions route emits
// round-trip cleanly: the plain `{"detail":"..."}` string the
// HTTPException branch emits, and the `{"detail":[...]}` list shape
// pydantic's validation envelope produces. decodeDetailString below
// only surfaces the string form; the list form falls back to the raw
// body so the operator sees the validation context.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body when it's a plain string. Falls back to the raw body when the
// JSON shape doesn't match (the pydantic validation envelope's
// `detail` is a list, not a string).
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	return strings.TrimSpace(body)
}

// loadBodyFlag reads the --body flag value supporting inline text,
// `@<path>` for file references, and `@-` for stdin. Returns the
// loaded body or an error. The backend's `body` field has a min_length=1
// constraint, so an empty result is rejected at the CLI rather than
// after a 422 round-trip.
//
// `@-` reads from cmd.InOrStdin() so tests can wire a bytes.Buffer; the
// read is bounded by `loadBodyStdinCap` to protect against an
// adversarial pipe.
//
// Trailing newlines (LF or CRLF) are stripped from file / stdin reads
// so a 1-line file passed via `@` doesn't carry a gratuitous trailing
// newline through the JSON body. Embedded newlines inside the text are
// preserved; only the final CRLF / LF is stripped. Inline text values
// pass through verbatim.
//
// Mirrors `loadBodyFlag` in cli/internal/cmd/kb/kb.go so the surface
// shape (`--body @file` / `--body @-` / `--body "inline"`) is the same
// across the operator-facing verb trees.
func loadBodyFlag(cmd *cobra.Command, raw string) (string, error) {
	if raw == "" {
		return "", errors.New("--body is required (inline text, @<path>, or @-)")
	}
	if !strings.HasPrefix(raw, "@") {
		return raw, nil
	}
	path := strings.TrimPrefix(raw, "@")
	if path == "-" {
		blob, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), loadBodyStdinCap+1))
		if err != nil {
			return "", fmt.Errorf("read --body from stdin: %w", err)
		}
		if int64(len(blob)) > loadBodyStdinCap {
			return "", fmt.Errorf(
				"--body from stdin exceeds %d-byte cap; pass a file with @<path> instead",
				loadBodyStdinCap,
			)
		}
		out := strings.TrimRight(string(blob), "\r\n")
		if out == "" {
			return "", errors.New("--body @- read empty input; cannot create convention with empty body")
		}
		return out, nil
	}
	blob, err := readBodyFile(path)
	if err != nil {
		return "", err
	}
	out := strings.TrimRight(string(blob), "\r\n")
	if out == "" {
		return "", fmt.Errorf("--body file %q is empty; cannot create convention with empty body", path)
	}
	return out, nil
}

// loadBodyStdinCap bounds the @- read so an adversarial / malformed
// pipe can't pin a create / edit verb in unbounded ReadAll. 256 KiB is
// generous for any realistic convention body (operational rules are
// bounded by the preamble token budget; reference material is the
// loose-bound case).
const loadBodyStdinCap int64 = 256 << 10

// readBodyFile is the file-read seam — split out so conventions_test.go
// can stub it deterministically without touching the filesystem.
var readBodyFile = func(path string) ([]byte, error) {
	blob, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read --body file %q: %w", path, err)
	}
	return blob, nil
}

// confirmPrompt prompts on stdin/stdout with the given message and
// returns true only when the operator types y/yes/Y. EOF (closed
// stdin) is treated as a no — scripted use must pass --confirm.
// Mirrors the kb / agent package's confirm helper.
func confirmPrompt(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.OutOrStdout(), "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		// Most common error: io.EOF from a piped empty stdin. Treat as
		// "no" so scripts that pipe /dev/null don't accidentally delete
		// a convention.
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// runEditor lives in editor.go alongside the other editor-related
// helpers — keep this file focused on the HTTP / auth helpers shared
// across the verb tree.
