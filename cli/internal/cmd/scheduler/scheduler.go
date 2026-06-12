// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package scheduler hosts the cobra commands under `meho scheduler ...`
// for G11.3-T5 (#826) of Initiative #804 (the P2 scheduler). v0.2 ships
// three operator-facing verbs that wrap the T5 REST surface
// (`backend/src/meho_backplane/api/v1/scheduler.py`):
//
//   - `meho scheduler list [--kind <kind>] [--status <s>] [--tenant <id>]
//     [--limit N] [--offset N] [--json]` — list triggers via
//     GET /api/v1/scheduler/triggers. Role: operator (operator role is
//     locked to its own tenant; --tenant is a tenant_admin-only filter).
//   - `meho scheduler create --kind <kind> --agent-definition <id>
//     [--cron-expr <expr>] [--fire-at <ts>] [--event-filter <json>]
//     [--timezone <tz>] [--inputs <json>] [--identity-sub <sub>]
//     [--in-flight-policy <p>] [--tenant <id>] [--json]` — create one
//     trigger via POST /api/v1/scheduler/triggers. Role: tenant_admin.
//   - `meho scheduler cancel <trigger_id> [--tenant <id>] [--json]` —
//     cancel one trigger via DELETE /api/v1/scheduler/triggers/{id}.
//     Role: tenant_admin.
//
// Authentication piggybacks on the token meho login wrote — same
// pattern as `meho agent`, `meho kb`, etc. RBAC at the backend rejects
// non-tenant_admin write callers with HTTP 403; the verbs render this
// as insufficient_role.
//
// G0.12-T13 #1271 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of backend pydantic models.
// Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and
// the verbs call the typed `*WithResponse` methods
// (`ListTriggersApiV1SchedulerTriggersGetWithResponse` etc.).
// Consumer-side struct drift — the #1069 root cause Initiative
// #1118 targets — can't recur because we now consume
// `api.ScheduledTriggerRead`, `api.ScheduledTriggerListResponse`,
// and `api.ScheduledTriggerCreate` directly.
package scheduler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// jsonObjectCap bounds a --event-filter / --inputs @<path> or @- read
// so an adversarial / malformed file or pipe can't pin a create verb in
// unbounded ReadAll. 256 KiB is generous for any realistic filter or
// payload.
const jsonObjectCap int64 = 256 << 10

// readJSONFile is the file-read seam — a var so the unit tests can stub
// it deterministically without touching the filesystem. The
// implementation enforces jsonObjectCap so a multi-GiB JSON file
// passed via `@<path>` cannot OOM the CLI (review M4 on PR #1128).
// It mirrors the stdin branch's io.LimitReader discipline.
var readJSONFile = func(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	read, err := io.ReadAll(io.LimitReader(f, jsonObjectCap+1))
	if err != nil {
		return nil, err
	}
	if int64(len(read)) > jsonObjectCap {
		return nil, fmt.Errorf("file %q exceeds %d-byte cap", path, jsonObjectCap)
	}
	return read, nil
}

// loadJSONObjectFlag reads a flag value that carries a JSON object,
// supporting inline JSON, `@<path>` for a file, and `@-` for stdin. It
// returns nil for an empty value so the caller can omit the field from
// the request body entirely. The decoded value must be a JSON object;
// a non-object (array, scalar, or JSON `null`) is rejected at the CLI
// rather than after a remote 422. Mirrors the agent package's helper
// of the same name. JSON `null` is rejected explicitly because
// json.Unmarshal of `null` into map[string]any sets the map to nil
// without returning an error -- a silent accept would forward an
// empty body field that the backend cannot disambiguate from
// "omitted" (review M3 on PR #1128).
func loadJSONObjectFlag(cmd *cobra.Command, raw, flagName string) (map[string]any, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var blob []byte
	switch {
	case raw == "@-":
		read, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), jsonObjectCap+1))
		if err != nil {
			return nil, fmt.Errorf("read %s from stdin: %w", flagName, err)
		}
		if int64(len(read)) > jsonObjectCap {
			return nil, fmt.Errorf("%s from stdin exceeds %d-byte cap", flagName, jsonObjectCap)
		}
		blob = read
	case strings.HasPrefix(raw, "@"):
		path := strings.TrimPrefix(raw, "@")
		read, err := readJSONFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s file %q: %w", flagName, path, err)
		}
		blob = read
	default:
		blob = []byte(raw)
	}
	var out map[string]any
	if err := json.Unmarshal(blob, &out); err != nil {
		return nil, fmt.Errorf("%s must be a JSON object: %w", flagName, err)
	}
	if out == nil {
		return nil, fmt.Errorf("%s must be a JSON object (got null)", flagName)
	}
	return out, nil
}

// NewRootCmd returns the `meho scheduler` parent command. Grafted onto
// the top-level meho tree by cmd/root.go alongside `meho agent`,
// `meho kb`, etc.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "scheduler",
		Short: "Manage scheduled triggers (list / create / cancel)",
		Long: "Manage tenant-scoped scheduled triggers wired by G11.3. " +
			"A scheduled trigger fires a P1 agent run on a cron " +
			"expression (kind=cron), at a single wall-clock instant " +
			"(kind=one_off), or on a matching MEHO-internal event " +
			"(kind=event; outbox-backed via G11.3-T3). Write verbs " +
			"(create / cancel) require tenant_admin; the read verb " +
			"(list) is operator-level. Tenant scoping is enforced " +
			"server-side via the JWT; tenant_admin callers may use " +
			"--tenant to act on another tenant's triggers.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newCancelCmd())
	return cmd
}

// validKinds mirrors the backend ScheduledTriggerKind enum. Kept as a
// plain-string set so the per-verb pre-check (rejects unknown --kind
// before the round-trip) can be exercised in tests without parsing the
// generated `api.ScheduledTriggerKind` constants — the wire-level
// values are the canonical truth.
var validKinds = map[string]bool{"cron": true, "one_off": true, "event": true}

// validStatuses mirrors the backend ScheduledTriggerStatus enum.
var validStatuses = map[string]bool{
	"active":    true,
	"paused":    true,
	"cancelled": true,
	"fired":     true,
}

// validInFlightPolicies mirrors the backend ScheduledTriggerInFlightPolicy enum.
var validInFlightPolicies = map[string]bool{
	"fail_into_audit": true,
	"resume":          true,
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its `access_token` field is empty.
// It's a credential-state failure rather than a transport failure, so
// renderRequestError maps it to auth_expired (exit 2) with a `meho
// login` hint — not unreachable (exit 3). Mirrors the shape adopted
// by the sibling typed-client migrations on Initiative #1118 (T1
// #1251 approvals, T4 #1262 agent-principal, T9 #1267 kb, T12 #1270
// retrieval).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the scheduler verb tree's
// transport will read off any backplane response body before
// surfacing `*http.MaxBytesError`. 1 MiB is generous for a paginated
// trigger list (limit 500 with full row payloads stays well under
// even with verbose `event_filter` JSON / `inputs` maps).
//
// Without the cap, an adversarial or runaway backplane response
// could OOM the CLI because the generated `Parse*Response` helpers
// call `io.ReadAll(rsp.Body)` on an unbounded body before
// constructing the typed envelope. The cap is installed at the
// transport layer via an inline `capRoundTripper` so it applies
// uniformly to every typed verb on the same `AuthedClient`. The kb
// / memory siblings install the same cap via
// `api.AuthedClientOptions.ResponseBodyLimit`; we duplicate the
// wrapper locally rather than reach into `cli/internal/api/client.go`
// to keep this PR's blast radius inside the scheduler verb tree
// (per Initiative #1118's "blast radius = touched files" discipline
// — the shared options struct will land separately when the sibling
// PRs settle).
const responseBodyCap int64 = 1 << 20

// capRoundTripper wraps an http.RoundTripper so every response body
// is re-bound to an http.MaxBytesReader before the typed-client
// parsers (oapi-codegen's generated `Parse*Response` helpers, which
// `io.ReadAll(rsp.Body)` to populate `*Response.Body []byte`) get a
// chance to drain it. A read at or past `limit` surfaces as
// `*http.MaxBytesError`, which `renderRequestError` maps to
// `output.Unexpected` (exit 4 — `unexpected_response`) rather than
// `output.Unreachable` (exit 3 — `network_unreachable`).
//
// The `*http.MaxBytesError` shape was added in Go 1.19 and is the
// canonical signal for "transport refused to read past N bytes."
// The wrapper applies the cap server-wide on the underlying
// transport so every typed verb on the same AuthedClient inherits
// it uniformly.
type capRoundTripper struct {
	base  http.RoundTripper
	limit int64
}

func (c *capRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := c.base.RoundTrip(req)
	if err != nil {
		return resp, err
	}
	if resp.Body != nil && c.limit > 0 {
		// http.MaxBytesReader returns an io.ReadCloser whose Close
		// closes the underlying body, so the existing close
		// discipline on the caller (oapi-codegen's `defer
		// rsp.Body.Close()` inside every `*WithResponse` method)
		// still drains the original body cleanly.
		resp.Body = http.MaxBytesReader(nil, resp.Body, c.limit)
	}
	return resp, nil
}

// cappedHTTPClient returns an http.Client whose Transport caps every
// response body at responseBodyCap. The clone keeps Timeout / Jar /
// CheckRedirect intact and only swaps the Transport for the capped
// wrapper so callers don't mutate http.DefaultClient (which is
// process-global). Passing the returned client to
// `api.AuthedClientOptions.HTTPClient` threads the cap through both
// the bearer-injecting editor and the oauth2 refresh exchange.
func cappedHTTPClient(base *http.Client) *http.Client {
	if base == nil {
		base = http.DefaultClient
	}
	clone := *base
	transport := clone.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}
	clone.Transport = &capRoundTripper{base: transport, limit: responseBodyCap}
	return &clone
}

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL with the 1 MiB response-body cap installed at the
// transport layer, and verifies a non-empty bearer is loaded. The
// caller forwards any returned error to renderRequestError for
// category mapping. Mirrors the sibling verb-tree migrations
// (G0.12-T4 #1262, G0.12-T9 #1267, G0.12-T12 #1270).
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{
		HTTPClient: cappedHTTPClient(nil),
	})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors
// the behaviour `api.AuthedClient.GetHealth` implements for the
// /api/v1/health endpoint, generalised so every scheduler verb runs
// the same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope (the
// generated *Response types expose StatusCode() through their
// embedded *http.Response). A nil response counts as "no retry" —
// the transport already failed and the caller surfaces err directly.
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

// renderRequestError translates a transport-layer request error into
// the right output.StructuredError category. Maps the scheduler REST
// surface's pre-response failures: missing bearer, no-refresh-token,
// token-not-found, body-cap / parse failures bubbling out of the
// generated `*WithResponse` parsers, plus the generic transport-down
// case. Non-2xx status codes carried in a typed response envelope
// are classified by renderHTTPStatus instead.
//
// Parse / cap failures route to `output.Unexpected` (exit 4 —
// `unexpected_response`) rather than `output.Unreachable` (exit 3 —
// `network_unreachable`). A 1 MiB body cap firing or a JSON decode
// rejecting a malformed payload is a contract / shape failure on
// the server side, not a transport-down failure on the operator's
// side; surfacing it as "unreachable" would send operators chasing
// a network ghost.
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
	// Transport-layer body-cap firing (*http.MaxBytesError out of
	// the capRoundTripper) and JSON shape failures bubbling out of
	// the generated parsers are server-side contract failures, not
	// transport-down failures — surface them as unexpected_response
	// (exit 4) with the backplane URL so the operator sees the
	// origin without chasing a network ghost.
	var maxBytesErr *http.MaxBytesError
	var syntaxErr *json.SyntaxError
	var unmarshalErr *json.UnmarshalTypeError
	if errors.As(err, &maxBytesErr) ||
		errors.As(err, &syntaxErr) ||
		errors.As(err, &unmarshalErr) ||
		errors.Is(err, io.ErrUnexpectedEOF) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: %v", backplaneURL, err)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx response carried in the
// typed envelope into the right StructuredError category. Mirrors
// the pre-migration `renderHTTPError` switch but acts on the
// (statusCode, body) pair lifted off the generated
// `*Response.HTTPResponse` + `Body` fields rather than a sentinel
// value. Scheduler-route-specific notes:
//
//   - 403 — write to a trigger the operator's role can't reach (the
//     operator-role caller using --tenant lands here too).
//   - 404 — `trigger_not_found` covers both genuine absence and
//     cross-tenant probes; the existence of a trigger is **not**
//     leaked across tenants via a 403/404 differential.
//   - 409 — `trigger_already_fired` on a cancel against a terminal
//     one-off trigger (lifecycle is `fired → end`, not
//     `fired → cancelled`).
//   - 422 — FastAPI validation envelope (invalid cron, unknown
//     `agent_definition_id`, malformed body); the verb surfaces
//     the backend's detail so the operator sees the field that
//     failed.
//   - 401 — backplane rejected the stored token after a refresh
//     attempt; auth_expired with a `meho login` hint.
//   - Other non-2xx — `Unexpected` carrying the raw body so the
//     operator sees an actionable signal.
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
		// `scheduler cancel` on an absent id surfaces here;
		// `trigger_not_found` covers both genuine absence and
		// cross-tenant probes (no existence leak across tenants).
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		// `trigger_already_fired` on a cancel against a terminal one-off.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", bodyStr)),
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

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error body.
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

// printTriggerSummary renders one trigger as a key-value summary.
// Consumes `api.ScheduledTriggerRead` directly so the generated
// typed envelope is the single source of truth for the on-screen
// shape; drift in the backend pydantic model now surfaces here at
// `go build` time rather than at runtime via the freshness gate.
func printTriggerSummary(w io.Writer, t *api.ScheduledTriggerRead) {
	if t == nil {
		return
	}
	fmt.Fprintf(w, "%-22s %s\n", "id:", t.Id.String())
	fmt.Fprintf(w, "%-22s %s\n", "tenant_id:", t.TenantId.String())
	fmt.Fprintf(w, "%-22s %s\n", "agent_definition_id:", t.AgentDefinitionId.String())
	fmt.Fprintf(w, "%-22s %s\n", "kind:", string(t.Kind))
	fmt.Fprintf(w, "%-22s %s\n", "status:", string(t.Status))
	fmt.Fprintf(w, "%-22s %s\n", "in_flight_policy:", string(t.InFlightPolicy))
	if t.CronExpr != nil {
		fmt.Fprintf(w, "%-22s %s\n", "cron_expr:", *t.CronExpr)
		fmt.Fprintf(w, "%-22s %s\n", "timezone:", t.Timezone)
	}
	if t.FireAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "fire_at:", formatTime(t.FireAt))
	}
	if t.NextFireAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "next_fire_at:", formatTime(t.NextFireAt))
	}
	if t.LastFiredAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "last_fired_at:", formatTime(t.LastFiredAt))
	}
	fmt.Fprintf(w, "%-22s %s\n", "identity_sub:", t.IdentitySub)
	fmt.Fprintf(w, "%-22s %s\n", "created_by:", t.CreatedBySub)
	fmt.Fprintf(w, "%-22s %s\n", "created_at:", formatTime(&t.CreatedAt))
}

// formatTime renders a *time.Time the way the backend ships it on
// the wire: ISO 8601 with sub-second precision when present. Go's
// stdlib `time.RFC3339Nano` layout matches what FastAPI emits for a
// `datetime` field (which uses Python's `isoformat()` and preserves
// microsecond precision). Rendering it here keeps the human-readable
// summary's wall-clock strings byte-for-byte aligned with the
// pre-migration output (which passed the JSON string through
// untouched).
func formatTime(t *time.Time) string {
	if t == nil {
		return ""
	}
	return t.Format(time.RFC3339Nano)
}
