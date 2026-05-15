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
	"github.com/evoila/meho/cli/internal/auth"
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
		Short: "Query the MEHO audit log (query / recent / show / who-touched / my-recent)",
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

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" from a generic resolver
// failure. Same shape as the helpers in
// cli/internal/cmd/targets/targets.go and operation/operation.go;
// kept independent because importing those packages here would
// create cycles via cmd/root.go.
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane resolves the backplane URL from either the
// `--backplane` override or the `meho login` config file. Same shape
// as the targets / operation package helpers — duplicated to avoid
// the import cycle the cmd → cmd/audit → cmd path would create.
func resolveBackplane(override string) (string, error) {
	if override != "" {
		return normaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &errNoBackplaneConfigured{inner: err}
		}
		return "", err
	}
	return normaliseURL(cfg.BackplaneURL)
}

// classifyBackplaneError maps a resolveBackplane error to the right
// output.StructuredError category.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes and parses the URL to fail
// fast on garbage input. Mirrors normaliseURL in
// targets/targets.go and operation/operation.go.
func normaliseURL(s string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
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

	raw, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20)) // 1 MiB cap
	if readErr != nil {
		return nil, fmt.Errorf("read response: %w", readErr)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

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
