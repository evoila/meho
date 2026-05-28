// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package audit hosts the cobra commands under `meho audit ...` for
// G8.1-T3 (#467) of Initiative #334. v0.2 ships six operator-facing
// verbs that wrap the T2 REST surface (#466) shipped by
// `backend/src/meho_backplane/api/v1/audit.py`:
//
//   - `meho audit query [--target T] [--principal P] [--op-id PATTERN]
//     [--op-class C] [--result-status S] [--since DUR] [--until DUR]
//     [--limit N] [--cursor C] [--json]` — full filter via
//     POST /api/v1/audit/query.
//   - `meho audit recent [--limit N] [--json]` — shortcut over
//     POST /api/v1/audit/query with since="24h" bound at the CLI.
//   - `meho audit show <audit-id> [--json]` — single-row detail via
//     GET /api/v1/audit/show/{audit_id}. 404 surfaces as
//     "audit row not found" — the cross-tenant probe always reads as
//     not-found (the backend enforces tenant-scoping at the substrate
//     layer and the route returns 404 rather than 403 so existence
//     never leaks).
//   - `meho audit who-touched <target> [--since DUR] [--limit N]
//     [--json]` — pre-canned shortcut via
//     GET /api/v1/audit/who-touched/{target}.
//   - `meho audit my-recent [--since DUR] [--limit N] [--json]` —
//     pre-canned shortcut filtered to the operator's own JWT subject
//     via GET /api/v1/audit/my-recent.
//   - `meho audit replay <session-id> [--json] [--max-depth N]` —
//     parent/child audit tree via
//     GET /api/v1/audit/sessions/{session_id}/replay.
//
// Each verb wraps one backplane route, renders the response either
// as a human-readable table / key-value summary or as raw JSON when
// `--json` is set. Authentication piggybacks on the token meho login
// wrote — same pattern as `meho targets` and `meho retrieval`.
//
// G0.12-T5 #1263 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of backend pydantic models.
// Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and
// the verbs call the typed `*WithResponse` methods
// (`QueryApiV1AuditQueryPostWithResponse`,
// `ShowApiV1AuditShowAuditIdGetWithResponse`,
// `ReplayApiV1AuditSessionsSessionIdReplayGetWithResponse`,
// `MyRecentApiV1AuditMyRecentGetWithResponse`,
// `WhoTouchedApiV1AuditWhoTouchedTargetGetWithResponse`).
// Consumer-side struct drift can't recur because we now consume
// `api.AuditEntry` / `api.AuditQueryResult` / `api.AuditReplayResult`
// / `api.ReplayNode` directly.
package audit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"
	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho audit` parent command. The command is
// grafted onto the top-level meho command tree by cmd/root.go
// alongside `meho operation`, `meho retrieval`, `meho targets`, and
// `meho connector`. The parent itself takes no args and prints its
// own help; every piece of behaviour lives in the per-subcommand
// RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "audit",
		Short: "Query the MEHO audit log (query / recent / show / who-touched / my-recent / replay)",
		Long: "Query the WORM-grade audit log written by the backplane on " +
			"every authenticated request. Wraps the G8.1-T2 REST surface " +
			"(/api/v1/audit/*). All verbs are tenant-scoped via the operator's " +
			"JWT — cross-tenant queries are impossible by construction.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newQueryCmd())
	cmd.AddCommand(newRecentCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newWhoTouchedCmd())
	cmd.AddCommand(newMyRecentCmd())
	cmd.AddCommand(newReplayCmd())
	return cmd
}

// httpResponseError carries a non-2xx status from a typed-client
// `*WithResponse` call up to the verb's renderer. The typed-client
// surface returns non-2xx responses in-band on the `(*Response, nil)`
// tuple (transport-layer failures come back on the `(nil, err)`
// tuple instead) — we lift the HTTP-failure case to an error type so
// the call sites can use a single `if err != nil` branch and
// `errors.As` routes the right way (HTTP status → `renderHTTPStatus`,
// everything else → `renderTransportError`). See `routeRequestError`.
type httpResponseError struct {
	statusCode int
	body       []byte
}

func (e *httpResponseError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.statusCode, trimmedBody(e.body))
}

// newAuthedClient builds an `api.AuthedClient` and surfaces its
// construction-time errors as the right `output.StructuredError`
// category (auth_expired when no token was ever stored, else
// unexpected_response with the underlying error wrapped). Splits the
// boilerplate every verb here used to duplicate inline.
func newAuthedClient(
	ctx context.Context,
	cmd *cobra.Command,
	backplaneURL string,
	jsonOut bool,
) (*api.AuthedClient, error) {
	client, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, renderClientError(cmd, backplaneURL, err, jsonOut)
	}
	return client, nil
}

// renderClientError maps `api.NewAuthedClient` failures onto the
// structured-error envelope. `IsTokenNotFound` is the "operator
// never ran meho login" sentinel and surfaces as auth_expired with a
// `meho login` hint; anything else is a build-time failure of the
// authed transport itself (token store unreadable, etc.) and
// surfaces as unexpected_response so the operator sees the cause.
func renderClientError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	if api.IsTokenNotFound(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"no stored credentials for %s; run `meho login %s`",
				backplaneURL, backplaneURL,
			)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unexpected(fmt.Sprintf("build authed client for %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// routeRequestError is the single dispatcher every verb feeds an
// error from `postQuery` / `getEntry` / `getMyRecent` /
// `getWhoTouched` / `fetchReplay` into. The error is either an
// `*httpResponseError` (the backplane responded with a non-2xx
// status) or a transport-layer failure (network, refresh-impossible,
// etc.); we route the former through `renderHTTPStatus` and the
// latter through `renderTransportError`.
func routeRequestError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
	var he *httpResponseError
	if errors.As(err, &he) {
		return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, jsonOut)
	}
	return renderTransportError(cmd, backplaneURL, err, jsonOut)
}

// renderTransportError maps a generated-client call's transport-layer
// error (network failure, refresh-impossible after a 401) onto the
// right structured-error category. The typed-client surface returns
// `(nil, err)` for these; non-2xx HTTP responses arrive as
// `(*Response, nil)` and are routed through `renderHTTPStatus` instead.
func renderTransportError(
	cmd *cobra.Command,
	backplaneURL string,
	err error,
	jsonOut bool,
) error {
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

// renderHTTPStatus classifies a non-2xx HTTP status (lifted off the
// generated `*Response.HTTPResponse.StatusCode` + `.Body` fields)
// into the right StructuredError category. Mirrors the audit-specific
// ladder the pre-G0.12-T5 `renderHTTPError` enforced:
//
//   - 401 (refresh failed) → auth_expired with a `meho login` hint.
//   - 403 (RBAC denial) → insufficient_role; the backend's 403 detail
//     names the required role.
//   - 400 → unexpected with the parser / substrate error message —
//     `DurationParseError`, `InvalidCursorError`, and
//     `UnsupportedFilterError` (v0.2's `parent_audit_id` /
//     `agent_session_id` gap) all surface as 400 from the backend.
//   - 404 → unexpected with "audit row not found" (only the
//     show endpoint emits this; cross-tenant probes also surface
//     here per the substrate's tenant-scoping discipline).
//   - 413 → defensive fallback to a session-too-large hint; the
//     replay verb intercepts 413 before reaching here so it can
//     append the session id to the `meho audit query` redirect.
//   - 422 → unexpected with the FastAPI validation envelope.
//   - Any other 4xx/5xx → unexpected with the raw body.
func renderHTTPStatus(
	cmd *cobra.Command,
	backplaneURL string,
	statusCode int,
	body []byte,
	jsonOut bool,
) error {
	bodyStr := trimmedBody(body)
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
	case http.StatusBadRequest:
		// Audit-API 400 = DurationParseError (router) /
		// InvalidCursorError (substrate) / UnsupportedFilterError
		// (substrate). The backend's HTTPException detail is the
		// parser's own message; surface it verbatim.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// Only the show endpoint emits 404. Cross-tenant probes
		// land here too — the substrate returns zero rows, the
		// route surfaces 404 (not 403) so existence never leaks.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("audit row not found"),
			jsonOut,
		)
	case http.StatusRequestEntityTooLarge:
		// Defensive fallback for any caller that didn't intercept
		// the replay-specific 413 before reaching here. The replay
		// verb's `renderReplayRequestError` shadows this arm so it
		// can append the session id to the `meho audit query
		// --session-id <id>` redirect.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"session too large to replay (%s rows, cap %d); "+
					"use `meho audit query --session-id <id>` to page the flat rows",
				decodeSessionTooLargeRowCount(bodyStr), replayRowCap)),
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

// trimmedBody renders a response body for inclusion in an error
// envelope: trims trailing whitespace, surfaces a placeholder when
// the backend returned an empty body so the operator-facing string
// is never just "HTTP 500:".
func trimmedBody(body []byte) string {
	s := string(body)
	for len(s) > 0 {
		last := s[len(s)-1]
		if last == ' ' || last == '\n' || last == '\r' || last == '\t' {
			s = s[:len(s)-1]
			continue
		}
		break
	}
	if s == "" {
		return "(empty body)"
	}
	return s
}

// replayRowCap mirrors the backend `_REPLAY_ROW_CAP`
// (`backend/src/meho_backplane/api/v1/audit.py`): the maximum number of
// anchor rows a single session may carry before the replay route's
// count-first guard refuses with 413 `session_too_large`. Surfaced in
// the CLI's 413 redirect so the operator sees the threshold they
// crossed. Kept in sync by hand — the value is part of the route's
// public contract (the 413 body always quotes the actual `row_count`,
// so a drift here only mis-renders the *cap* in the hint, never the
// real count).
const replayRowCap = 10000

// sessionTooLargeDetail models the structured 413 body the replay route
// emits: `{"detail": {"detail": "session_too_large", "row_count": N}}`.
// FastAPI wraps the route's `HTTPException(detail=...)` dict under a
// top-level `detail` key, so the row count lives two levels deep.
type sessionTooLargeDetail struct {
	Detail struct {
		Detail   string      `json:"detail"`
		RowCount json.Number `json:"row_count"`
	} `json:"detail"`
}

// decodeSessionTooLargeRowCount pulls the `row_count` out of a 413
// `session_too_large` body. Returns the count as a string (so a 64-bit
// count renders exactly) or "?" when the body isn't the expected shape
// — the redirect stays useful even if the backend changes the envelope.
func decodeSessionTooLargeRowCount(body string) string {
	var d sessionTooLargeDetail
	if err := json.Unmarshal([]byte(body), &d); err == nil {
		if rc := d.Detail.RowCount.String(); rc != "" {
			return rc
		}
	}
	return "?"
}

// detailEnvelope models FastAPI's HTTPException JSON shape.
type detailEnvelope struct {
	Detail json.RawMessage `json:"detail"`
}

// decodeDetailString pulls the `detail` field out of a FastAPI error
// body when it's a plain string. Falls back to the raw body when the
// JSON shape doesn't match.
func decodeDetailString(body string) string {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	// Trim trailing whitespace defensively when falling back to the
	// raw body — keeps the operator-visible message stable when the
	// backend appends a newline.
	for len(body) > 0 && (body[len(body)-1] == ' ' || body[len(body)-1] == '\n' ||
		body[len(body)-1] == '\r' || body[len(body)-1] == '\t') {
		body = body[:len(body)-1]
	}
	return body
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Same shape as the sibling-package helper.
func truncate(s string, maxLen int) string {
	if maxLen < 1 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	return string(runes[:maxLen-1]) + "…"
}

// strDeref returns *s or empty string when s is nil. Backend Pydantic
// models declare many audit fields as `Optional[...]`; null lands as
// nil after JSON decode and the formatter consumes the empty string.
func strDeref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// uuidDeref returns u.String() or empty string when u is nil. The
// generated client surfaces nullable UUID fields as `*openapi_types.UUID`
// (= `*uuid.UUID`); the renderers consume them via this helper so the
// "-" placeholder rendering stays in one place.
func uuidDeref(u *openapi_types.UUID) string {
	if u == nil {
		return ""
	}
	return u.String()
}

// formatTS renders an `api.AuditEntry.Ts` (`time.Time`) as the
// RFC3339-with-nanos string the pre-migration `Entry.TS` string
// field carried verbatim. The backend emits the column as an
// ISO-8601 string; the generated client decodes it into `time.Time`
// via the JSON unmarshal default; the renderer re-serialises with
// the same precision so the operator-visible column matches the
// pre-migration shape. UTC is the substrate-canonical zone.
func formatTS(ts time.Time) string {
	return ts.UTC().Format(time.RFC3339Nano)
}
