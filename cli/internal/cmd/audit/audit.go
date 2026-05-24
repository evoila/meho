// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package audit hosts the cobra commands under `meho audit ...` for
// G8.1-T3 (#467) of Initiative #334. v0.2 ships five operator-facing
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
//
// Each verb wraps one backplane route, renders the response either
// as a human-readable table / key-value summary or as raw JSON when
// `--json` is set. Authentication piggybacks on the token meho login
// wrote — same pattern as `meho targets` and `meho retrieval`.
package audit

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

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

// Entry mirrors the backend `AuditEntry` Pydantic model
// (`backend/src/meho_backplane/audit_query/schemas.py`). Fields are
// hand-written rather than aliased to a generated client type so the
// audit package stays decoupled from oapi-codegen churn — the
// targets / retrieval / operation packages take the same stance.
//
// Optional Pydantic fields land as `*string` so the JSON round-trip
// preserves the explicit-null wire shape. `DurationMS` is a string
// because Pydantic v2 serialises `Decimal` as a quoted decimal string
// by default.
type Entry struct {
	ID               string         `json:"id"`
	TS               string         `json:"ts"`
	TenantID         *string        `json:"tenant_id"`
	PrincipalSub     string         `json:"principal_sub"`
	PrincipalName    *string        `json:"principal_name"`
	TargetID         *string        `json:"target_id"`
	TargetName       *string        `json:"target_name"`
	Method           string         `json:"method"`
	Path             string         `json:"path"`
	StatusCode       int            `json:"status_code"`
	RequestID        *string        `json:"request_id"`
	DurationMS       *string        `json:"duration_ms"`
	Payload          map[string]any `json:"payload"`
	OpID             string         `json:"op_id"`
	OpClass          string         `json:"op_class"`
	ResultStatus     string         `json:"result_status"`
	ParentAuditID    *string        `json:"parent_audit_id"`
	AgentSessionID   *string        `json:"agent_session_id"`
	BroadcastEventID *string        `json:"broadcast_event_id"`
}

// QueryResult mirrors the backend `AuditQueryResult` Pydantic model
// — a page of audit rows plus the opaque forward-only cursor.
// `NextCursor` keeps the JSON key on null (no omitempty) so the wire
// shape matches the Pydantic side, where the field is always emitted
// as `null` when there is no further page. The retire-checklist PR
// (#497) tripped on the same omitempty / schema-stability gotcha and
// the fix is to never drop the key on Go re-marshal.
type QueryResult struct {
	Rows       []Entry `json:"rows"`
	NextCursor *string `json:"next_cursor"`
}

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.StructuredError category.
// Same classification ladder as targets / operation packages with
// audit-specific 400 handling:
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
//   - 422 → unexpected with the FastAPI validation envelope.
//   - Any other 4xx/5xx → unexpected with the raw body.
//   - Pure transport errors → unreachable.
func renderRequestError(
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
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	var he *httpError
	if errors.As(err, &he) {
		return renderHTTPError(cmd, backplaneURL, he, jsonOut)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPError classifies a non-2xx response into the right
// StructuredError category.
func renderHTTPError(
	cmd *cobra.Command,
	backplaneURL string,
	he *httpError,
	jsonOut bool,
) error {
	switch he.StatusCode {
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
			output.InsufficientRole(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusBadRequest:
		// Audit-API 400 = DurationParseError (router) /
		// InvalidCursorError (substrate) / UnsupportedFilterError
		// (substrate). The backend's HTTPException detail is the
		// parser's own message; surface it verbatim.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
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
		// Only the replay endpoint emits 413 (session_too_large): a
		// session above the server's replayRowCap anchor rows. The
		// replay verb intercepts this status before reaching here so it
		// can append the `meho audit query --session-id <id>` redirect
		// (it knows the session id; this shared renderer does not). This
		// arm is the defensive fallback for any other caller — surface
		// the cardinality and the same query-verb pointer.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"session too large to replay (%s rows, cap %d); "+
					"use `meho audit query --session-id <id>` to page the flat rows",
				decodeSessionTooLargeRowCount(he.Body), replayRowCap)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", he.Body)),
			jsonOut,
		)
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, he.StatusCode, he.Body)),
			jsonOut,
		)
	}
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
	return strings.TrimSpace(body)
}

// doAuthedRequest issues a single HTTP request against the backplane
// with bearer injection and one-shot 401-refresh-retry. Returns the
// response body bytes (already drained) on 2xx, or an *httpError on
// non-2xx, or an error categorised by api.IsTokenNotFound /
// api.IsNoRefreshToken / generic transport.
//
// Mirrors cli/internal/cmd/targets/targets.go::doAuthedRequest and
// operation/operation.go's namesake — kept independent for the
// import-cycle reason called out on resolveBackplane.
func doAuthedRequest(
	ctx context.Context,
	backplaneURL, method, path string,
	body []byte,
) ([]byte, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
	if err != nil {
		return nil, err
	}
	httpClient := authed.HTTPClient()
	bearer := authed.AccessToken()
	if bearer == "" {
		return nil, errors.New("meho: stored token has no access_token")
	}

	resp, err := sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		if rerr := authed.Refresh(ctx); rerr != nil {
			resp.Body.Close()
			return nil, rerr
		}
		resp.Body.Close()
		bearer = authed.AccessToken()
		resp, err = sendRequest(ctx, httpClient, backplaneURL, method, path, bearer, body)
		if err != nil {
			return nil, err
		}
	}
	defer resp.Body.Close()

	// Read with a 1-MiB cap. The +1 byte over the cap is the
	// truncation-detection trick: if ReadAll returns more than
	// ``responseBodyCap`` bytes, the response was at least cap+1 bytes
	// long and the decoder would otherwise consume a silently-truncated
	// JSON payload. Fail loud instead — a truncated audit response
	// surfaces as "decode error: unexpected end of JSON input" without
	// this guard, which buries the real cause (response too large for
	// the chassis CLI's safety cap).
	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, responseBodyCap+1))
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if int64(len(raw)) > responseBodyCap {
		return nil, fmt.Errorf(
			"response body exceeds %d-byte cap; refusing to decode possibly-truncated JSON",
			responseBodyCap,
		)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// responseBodyCap is the hard upper bound on a backplane response
// body the CLI is willing to read. Audit pages cap at 1000 rows
// server-side; a typical row at ~500 B yields ~500 KiB, so 1 MiB
// is comfortable headroom. The cap protects against an
// adversarial / misconfigured backplane sending an unbounded
// response — the alternative is OOM. The +1-byte read pattern in
// “doAuthedRequest“ distinguishes "fits in the cap" from
// "truncated at the cap" so the decoder doesn't silently consume
// a half-JSON.
const responseBodyCap int64 = 1 << 20

// httpError carries a non-2xx response so per-verb runners can
// render the right category.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// sendRequest is the bottom of the stack: build the http.Request,
// stamp bearer + content headers, fire it.
func sendRequest(
	ctx context.Context,
	client *http.Client,
	backplaneURL, method, path, bearer string,
	body []byte,
) (*http.Response, error) {
	fullURL := backplaneURL + path
	var bodyReader io.Reader
	if body != nil {
		bodyReader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, fullURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return client.Do(req)
}

// pathEscape escapes a single path segment for use inside a backend
// URL.
func pathEscape(segment string) string {
	return url.PathEscape(segment)
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

// decodeAuditResponse JSON-decodes *raw* into *out* via a
// “json.Decoder“ configured with “UseNumber()“ so payload numbers
// survive as “json.Number“ rather than collapsing to “float64“.
// Audit payloads carry integer-valued fields (“hit_count“,
// “query_count“, timestamps stored as Unix seconds, etc.) that
// silently lose precision when decoded as IEEE-754 doubles past
// 2^53. The exact-integer-preserving path lets jq pipelines and the
// human-readable summary render the value the backend actually wrote
// instead of a rounded float. Used by every audit verb that decodes
// an “Entry“ or “QueryResult“.
func decodeAuditResponse(raw []byte, out any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(out); err != nil {
		return fmt.Errorf("decode audit response: %w", err)
	}
	return nil
}
