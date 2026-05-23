// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package memory hosts the top-level cobra commands `meho remember`,
// `meho recall`, `meho forget`, and `meho list` for G5.1-T4 (#424) of
// Initiative #332. The four verbs wrap the four REST routes shipped
// by G5.1-T2 (#422) plus the G0.4-T5 `/api/v1/retrieve` route for the
// `meho recall --query` retrieval form:
//
//   - `meho remember "body" [--scope SCOPE] [--slug SLUG]
//     [--target NAME] [--tag T] [--ttl 7d] [--json]` —
//     POST /api/v1/memory. Default scope `user-tenant`. Body can be
//     piped on stdin when the positional arg is `-`.
//   - `meho recall <scope>/<slug> [--target NAME] [--json]` —
//     GET /api/v1/memory/{scope}/{slug}.
//   - `meho recall --query "search terms" [--scope SCOPE] [--limit N]
//     [--json]` — POST /api/v1/retrieve with source="memory".
//   - `meho forget <scope>/<slug> [--target NAME] [--confirm]
//     [--json]` — DELETE /api/v1/memory/{scope}/{slug}.
//   - `meho list [--scope SCOPE] [--tag T] [--include-expired]
//     [--slug-pattern P] [--limit N] [--json]` —
//     GET /api/v1/memory.
//
// The verbs are registered as **top-level** cobra commands per the
// consumer-needs.md §G5 ergonomic shape (the consumer-facing CLI
// spec calls out `meho remember` and `meho recall`, not
// `meho memory remember`). The acceptance criterion in #424 names
// `meho list --scope user` verbatim; `meho memory list` would
// violate the contract.
//
// The implementation deliberately follows the in-package HTTP helper
// pattern the sibling verb trees use (one resolveBackplane /
// doAuthedRequest / renderRequestError trio per package) rather
// than a shared cli/internal/api_client package. The reason is the
// import-cycle one: each verb tree is registered onto the root
// command, so a shared helper imported from cmd/* and from any
// per-tree package would close the cycle. Duplicating the helpers
// (a handful of small, stable functions) is the convention every
// sibling package follows — see cli/internal/cmd/kb/kb.go's identical
// docstring for the matching justification.
package memory

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
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// Scope is the wire-level identifier matching the backend's
// MemoryScope StrEnum (`backend/src/meho_backplane/memory/schemas.py`
// L87-107). Five values; one per consumer-needs.md §G5 row. The
// string values are the same identifier the route path segment,
// `--scope` flag value, and the audit/broadcast contextvar carry.
//
// Hand-typed rather than aliased to a generated client type so the
// memory package stays decoupled from oapi-codegen churn — same
// stance every sibling package (kb, audit, targets, ...) takes for
// its mirrored enums.
type Scope string

const (
	// ScopeUser is the per-operator-across-tenants scope — a memory
	// only the writing operator can read, across every tenant they
	// belong to.
	ScopeUser Scope = "user"
	// ScopeUserTenant is the operator-within-one-tenant scope — the
	// default for `meho remember` because consumer-needs.md §G5 L137
	// identifies it as the most common case.
	ScopeUserTenant Scope = "user-tenant"
	// ScopeUserTarget is the operator-against-one-target scope —
	// requires `--target`.
	ScopeUserTarget Scope = "user-target"
	// ScopeTenant is the tenant-wide scope — write requires
	// `tenant_admin`; the substrate's RBAC matrix surfaces this as
	// 403 from the service.
	ScopeTenant Scope = "tenant"
	// ScopeTarget is the per-target-shared scope — every operator
	// touching one infrastructure target sees the same memory.
	// Requires `--target`.
	ScopeTarget Scope = "target"
)

// validScopes is the set the parseScope helper uses to reject typos
// client-side rather than relying on a 422 round-trip. Membership
// (not lookup) matters; the helper returns a Scope value verbatim
// when the input matches.
var validScopes = map[string]Scope{
	"user":        ScopeUser,
	"user-tenant": ScopeUserTenant,
	"user-target": ScopeUserTarget,
	"tenant":      ScopeTenant,
	"target":      ScopeTarget,
}

// targetScoped names the scope values where `--target` is required
// at the CLI layer (mirrors the backend's TARGET_SCOPED frozenset in
// `backend/src/meho_backplane/memory/schemas.py` L113). Failing fast
// client-side beats a 422 round-trip and makes the error message
// say what the operator needs to fix rather than relaying a server
// message that mentions backend-internal field names.
var targetScoped = map[Scope]bool{
	ScopeUserTarget: true,
	ScopeTarget:     true,
}

// Entry mirrors the backend `MemoryEntry` pydantic model
// (`backend/src/meho_backplane/memory/schemas.py` L152-177). One
// row as returned by GET /api/v1/memory/{scope}/{slug} and
// POST /api/v1/memory.
type Entry struct {
	ID         string         `json:"id"`
	TenantID   string         `json:"tenant_id"`
	Scope      Scope          `json:"scope"`
	Slug       string         `json:"slug"`
	Body       string         `json:"body"`
	Metadata   map[string]any `json:"metadata"`
	ExpiresAt  *string        `json:"expires_at"`
	UserSub    *string        `json:"user_sub"`
	TargetName *string        `json:"target_name"`
	CreatedAt  string         `json:"created_at"`
	UpdatedAt  string         `json:"updated_at"`
}

// ListResponse mirrors the backend `MemoryListResponse` envelope.
// Wrapped in `{"entries": [...]}` for forward-compat with future
// cursor / paging fields — same shape kb / connectors_ingest adopt.
type ListResponse struct {
	Entries []Entry `json:"entries"`
}

// RetrievalHit mirrors the backend `RetrievalHit` pydantic model
// (`backend/src/meho_backplane/retrieval/retriever.py`). Returned by
// POST /api/v1/retrieve. `BM25Score`, `CosineScore`, `BM25Rank`, and
// `CosineRank` are pointer-typed because the backend emits `null`
// for documents that did not appear in that signal's top-K
// candidate list.
type RetrievalHit struct {
	DocumentID  string         `json:"document_id"`
	TenantID    string         `json:"tenant_id"`
	Source      string         `json:"source"`
	SourceID    string         `json:"source_id"`
	Kind        string         `json:"kind"`
	Body        string         `json:"body"`
	DocMetadata map[string]any `json:"doc_metadata"`
	FusedScore  float64        `json:"fused_score"`
	BM25Score   *float64       `json:"bm25_score"`
	CosineScore *float64       `json:"cosine_score"`
	BM25Rank    *int           `json:"bm25_rank"`
	CosineRank  *int           `json:"cosine_rank"`
}

// RetrieveResponse mirrors the backend `RetrieveResponse` envelope.
type RetrieveResponse struct {
	Hits            []RetrievalHit `json:"hits"`
	QueryDurationMS float64        `json:"query_duration_ms"`
}

// rememberRequest mirrors the backend `RememberBody` pydantic model
// (`backend/src/meho_backplane/api/v1/memory.py` L174-211). Fields
// without JSON `omitempty` are mandatory at the wire level.
//
// `Persist` is a tri-state escape-hatch for the `expires_at` field
// the backend's G5.2-T2 (#624) default-TTL injection key on:
//
//   - Persist=false, ExpiresAt="" → field is OMITTED from the JSON;
//     the backend's :func:`_resolve_default_ttl` sees the field as
//     absent from :attr:`BaseModel.model_fields_set` and injects
//     “now + memory_user_default_ttl_days“ on “memory-user“ writes.
//   - Persist=false, ExpiresAt="<RFC3339>" → field is sent verbatim;
//     the backend honours it as the explicit cutoff.
//   - Persist=true → field is emitted as explicit JSON `null`; the
//     backend sees the field as present in
//     :attr:`BaseModel.model_fields_set` with value None and skips
//     the default. This is the wire shape `meho remember --persist`
//     uses to opt out of the auto-TTL and pin the memory forever.
//
// The marshaling can't ride on Go's `,omitempty` semantics alone
// because `*string`+`omitempty` collapses nil-pointer and empty-
// string into "omit", whereas the backend's contract distinguishes
// "absent field" (default fires) from "explicit null" (default
// skipped). The custom :func:`MarshalJSON` below is the load-bearing
// gate for the three-state discipline.
type rememberRequest struct {
	Scope      Scope
	Body       string
	Slug       string
	Metadata   map[string]any
	ExpiresAt  string
	TargetName string
	// Persist, when true, emits ``"expires_at": null`` on the wire
	// even when ExpiresAt is empty. Mutually exclusive with a non-
	// empty ExpiresAt in practice; the verb-level CLI gate refuses
	// `--persist` + `--ttl` together so the operator's intent is
	// unambiguous. The struct doesn't enforce that mutual exclusion
	// itself (callers do) -- when both are set, ExpiresAt wins
	// because emitting "null" alongside a real timestamp would be
	// nonsensical.
	Persist bool
}

// MarshalJSON renders rememberRequest with explicit handling for the
// G5.2-T2 (#624) tri-state “expires_at“ contract. See the type-level
// docstring for the three states and their wire shapes.
//
// Implementation notes:
//
//   - Uses a positional emit (json.Marshal over a map[string]any)
//     rather than struct tags so the `expires_at` rendering can branch
//     on the Persist bool without leaking into the public field set.
//   - The map ordering does not affect JSON correctness; the backend
//     parses fields by key, not position.
//   - "Persist + ExpiresAt set" lets ExpiresAt win (we already wrote
//     the operator's intent to a clock-aligned cutoff at the
//     parseTTLFlag stage; emitting `null` would silently override
//     their `--ttl` value). The verb-level mutual-exclusion gate is
//     where this collision is reported back to the operator.
func (r rememberRequest) MarshalJSON() ([]byte, error) {
	out := map[string]any{
		"scope": r.Scope,
		"body":  r.Body,
	}
	if r.Slug != "" {
		out["slug"] = r.Slug
	}
	if r.Metadata != nil {
		out["metadata"] = r.Metadata
	}
	if r.TargetName != "" {
		out["target_name"] = r.TargetName
	}
	switch {
	case r.ExpiresAt != "":
		out["expires_at"] = r.ExpiresAt
	case r.Persist:
		// Explicit JSON `null` so backend's
		// :func:`_resolve_default_ttl` sees "expires_at" in
		// :attr:`BaseModel.model_fields_set` and skips the default.
		out["expires_at"] = nil
	}
	return json.Marshal(out)
}

// retrieveRequest mirrors the backend `RetrieveRequest` pydantic
// model (`backend/src/meho_backplane/api/v1/retrieve.py` L102-120).
// `meho recall --query` pins `source="memory"` so the substrate
// only ranks memory-scoped rows.
type retrieveRequest struct {
	Query  string `json:"query"`
	Source string `json:"source,omitempty"`
	Kind   string `json:"kind,omitempty"`
	Limit  int    `json:"limit,omitempty"`
}

// errMissingAccessToken is the sentinel doAuthedRequest returns
// when the stored token row exists but its access_token field is
// empty. Routed to auth_expired (exit 2) with a `meho login` hint
// rather than the generic transport-error path. Mirrors the kb
// sibling's fix that landed in PR #500 review iteration 1.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap is the hard upper bound on a backplane response
// body the CLI is willing to read. Memory list pages cap at 500
// rows server-side; an average memory body is small (consumer-needs
// describes them as "useful behavioral preferences" not multi-KB
// blobs), so 1 MiB is comfortable headroom. The +1 byte read in
// doAuthedRequest distinguishes "fits in the cap" from "truncated
// at the cap" so a silently-truncated JSON payload never reaches
// the decoder.
const responseBodyCap int64 = 1 << 20

// loadBodyStdinCap bounds the `-` (stdin) read on `meho remember`
// so an adversarial / malformed pipe can't pin the verb in
// unbounded io.ReadAll. 1 MiB matches the response cap; memory
// bodies are expected to be hand-written notes, not megabyte
// blobs.
const loadBodyStdinCap int64 = 1 << 20

// httpError carries a non-2xx response so per-verb runners can
// render the right category. Same shape as the kb sibling.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.StructuredError category.
// Same classification ladder as kb / audit / targets, with the
// memory-route-specific 4xx handling lifted into renderHTTPError.
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
//
// Memory-route-specific notes:
//
//   - 403 — write to a scope the operator's tenant role can't reach.
//     The backend's detail string is `permission_denied: <reason>`;
//     surface verbatim so the operator sees the matrix mismatch
//     directly.
//   - 404 — read / delete against a missing slug. The memory recall
//     route deliberately collapses "missing" and "no access" into
//     404 to avoid leaking tenant boundaries via status-code
//     differential. Surface the detail (`memory_not_found`) verbatim.
//   - 422 — pydantic validation (invalid slug shape, missing required
//     body field, target_name missing for target-scoped writes).
//     Surface the validation envelope so the operator sees the
//     field that failed.
//   - 401 — backplane rejected the stored token after a refresh
//     attempt; auth_expired with a `meho login` hint.
//   - Pure transport errors fall through to unreachable.
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
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
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
// JSON shape doesn't match. Same shape as the kb sibling.
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
// Mirrors cli/internal/cmd/kb/kb.go::doAuthedRequest and its
// targets / operation / audit / connector siblings.
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
		return nil, errMissingAccessToken
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

	// Read with a 1-MiB cap and the +1 byte truncation-detection
	// trick — same shape as the kb sibling. A response that fills
	// the cap is treated as oversized rather than fed to the JSON
	// decoder where it would surface as "unexpected end of JSON
	// input" without naming the real cause.
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
	// DELETE /api/v1/memory/{scope}/{slug} returns 204 with no body
	// whether or not the row existed (idempotent contract — same
	// shape kb adopted; the conflation prevents tenant-probe by
	// status-code differential).
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// sendRequest is the bottom of the stack: build the http.Request,
// stamp bearer + content headers, fire it. Mirrors the kb sibling.
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
// URL. Mirrors the kb sibling.
func pathEscape(segment string) string {
	return url.PathEscape(segment)
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Rune-aware so multi-byte UTF-8 survives the
// cut. Mirrors the sibling-package helper.
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

// parseScope normalises a --scope flag value to a typed Scope or
// returns a descriptive error. Defaults are applied by the caller
// (Cobra's StringVar default), not here — this helper exists to
// reject typos client-side rather than relying on a 422 round-trip
// (which would carry a backend-internal error string mentioning
// pydantic types).
func parseScope(raw string) (Scope, error) {
	v, ok := validScopes[strings.TrimSpace(raw)]
	if !ok {
		return "", fmt.Errorf(
			"invalid --scope %q (allowed: user, user-tenant, user-target, tenant, target)",
			raw,
		)
	}
	return v, nil
}

// parseScopeSlugArg parses the `<scope>/<slug>` positional argument
// shared by `meho recall` and `meho forget`. The split keeps the
// scope and slug typed; an arg without `/` or with an empty side
// returns a clear error rather than a confusing 404 from the
// backend.
//
// We deliberately split on the FIRST `/` (strings.Cut) so a slug
// containing dots, hyphens, or underscores survives — the substrate's
// SLUG_PATTERN admits `[A-Za-z0-9_.-]+`, so a slug like
// `k8s.rollout-note` is legal and we must not mangle it. The
// substrate rejects slashes inside a slug, so any second `/` in the
// input is a syntax error the operator should fix.
func parseScopeSlugArg(arg string) (Scope, string, error) {
	trimmed := strings.TrimSpace(arg)
	if trimmed == "" {
		return "", "", errors.New("expected <scope>/<slug>; got empty argument")
	}
	scopePart, slugPart, ok := strings.Cut(trimmed, "/")
	if !ok {
		return "", "", fmt.Errorf(
			"expected <scope>/<slug>; got %q (missing '/' separator)", arg,
		)
	}
	scopePart = strings.TrimSpace(scopePart)
	slugPart = strings.TrimSpace(slugPart)
	if scopePart == "" || slugPart == "" {
		return "", "", fmt.Errorf(
			"expected <scope>/<slug>; got %q (empty scope or slug)", arg,
		)
	}
	if strings.Contains(slugPart, "/") {
		return "", "", fmt.Errorf(
			"slug %q contains '/' which is not allowed", slugPart,
		)
	}
	scope, err := parseScope(scopePart)
	if err != nil {
		return "", "", err
	}
	return scope, slugPart, nil
}

// parseTagsFlag parses the `--tag T` flag value(s) into a slice for
// inclusion under `metadata.tags`. Cobra's StringSliceVar already
// supports `--tag a --tag b`; the helper trims whitespace and drops
// empty strings so a bare `--tag ""` is ignored rather than
// silently smuggled into the JSON payload. Returns nil for an empty
// input slice so the caller can omit the field.
func parseTagsFlag(tags []string) []string {
	out := make([]string, 0, len(tags))
	for _, t := range tags {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		out = append(out, t)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// parseTTLFlag parses the `--ttl 7d` flag value into an ISO-8601
// timestamp suitable for the wire-level `expires_at` field. The
// helper accepts shorthand units (`s`/`m`/`h`/`d`) plus the
// `time.ParseDuration` set (`s`/`m`/`h`); pure-Go ParseDuration
// doesn't accept `d`, so the day suffix is handled explicitly.
// Returns the ISO-8601 expires_at as `time.Now().UTC()` plus the
// parsed duration so the backend sees a clock-aligned cutoff.
//
// Empty input returns ("", nil) so the caller can omit the field
// and let the substrate apply its own default (no automatic TTL in
// G5.1; G5.2 #374 ships the default-7-day injection on
// `memory-user` writes).
//
// The `now` parameter is injectable so tests can pin the reference
// time. Production callers pass `time.Now`.
func parseTTLFlag(raw string, now func() time.Time) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", nil
	}
	d, err := parseDurationShorthand(raw)
	if err != nil {
		return "", err
	}
	if d <= 0 {
		return "", fmt.Errorf("--ttl %q must be positive", raw)
	}
	expires := now().UTC().Add(d)
	return expires.Format(time.RFC3339), nil
}

// parseDurationShorthand extends time.ParseDuration with a `d`
// (days) suffix. The shape `7d` is the canonical TTL UX from the
// issue body's `--ttl 7d` example; ParseDuration only accepts
// h/m/s/ms/us/ns so days must be handled here.
//
// Accepts the same h/m/s units ParseDuration handles, so an
// operator who wants `--ttl 36h` doesn't need to think about
// whether the CLI rolled its own parser.
func parseDurationShorthand(raw string) (time.Duration, error) {
	if strings.HasSuffix(raw, "d") {
		var days int
		if _, err := fmt.Sscanf(raw, "%dd", &days); err != nil {
			return 0, fmt.Errorf("--ttl %q: %w", raw, err)
		}
		if days <= 0 {
			return 0, fmt.Errorf("--ttl %q: days must be positive", raw)
		}
		return time.Duration(days) * 24 * time.Hour, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("--ttl %q: %w (expected e.g. 30m, 24h, 7d)", raw, err)
	}
	return d, nil
}

// requireTargetForScope enforces the AC "scope=target/user-target
// requires --target" client-side. Returning a CLI error before the
// HTTP round-trip makes the operator's fix obvious — the alternative
// (a 422 from the backend with a pydantic-shaped error) leaks
// implementation details and reads as a server fault.
func requireTargetForScope(scope Scope, target string) error {
	if !targetScoped[scope] {
		return nil
	}
	if strings.TrimSpace(target) == "" {
		return fmt.Errorf("--target is required when --scope=%s", scope)
	}
	return nil
}

// loadBody resolves the positional body argument for `meho
// remember`. The shape mirrors `meho kb add --body`: inline text by
// default; `-` (the bare hyphen) reads from cmd.InOrStdin() so the
// issue's `echo "body" | meho remember` UX works. The substrate's
// `min_length=1` constraint surfaces as a client-side error rather
// than a 422 round-trip.
//
// `meho remember` differs from `meho kb add` in two ways:
//
//   - The body is the positional argument (not a --body flag).
//   - The stdin sentinel is `-` (bare hyphen), matching the UNIX
//     convention many CLIs use. kb's `--body @-` shape is flag-
//     prefixed because `meho kb add <slug> --body @-` carries the
//     slug as the positional; `meho remember` flips that — the
//     body is the positional, slug is `--slug`.
//
// Trailing newlines from stdin reads are stripped so a piped
// `echo "body"` doesn't carry the gratuitous `\n` echo appends.
// Embedded newlines inside the body are preserved.
func loadBody(cmd *cobra.Command, raw string) (string, error) {
	if raw == "-" {
		blob, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), loadBodyStdinCap+1))
		if err != nil {
			return "", fmt.Errorf("read body from stdin: %w", err)
		}
		if int64(len(blob)) > loadBodyStdinCap {
			return "", fmt.Errorf(
				"body from stdin exceeds %d-byte cap", loadBodyStdinCap,
			)
		}
		out := strings.TrimRight(string(blob), "\r\n")
		if out == "" {
			return "", errors.New("body from stdin is empty; cannot create memory with empty body")
		}
		return out, nil
	}
	if strings.TrimSpace(raw) == "" {
		return "", errors.New(
			"body is required (inline text or '-' to read from stdin)",
		)
	}
	return raw, nil
}

// confirmPrompt prompts the operator and returns true only when the
// answer is y/yes (case-insensitive). EOF (closed stdin) reads as no
// — scripted use must pass --confirm.
//
// The prompt text is written to `promptW`; callers pass stderr when
// `--json` is set so the JSON envelope on stdout stays parseable
// (`jq` consumers shouldn't have to skip past the prompt). Callers
// passing stdout for a human-facing prompt match the kb sibling's
// shape.
//
// Honours cmd.InOrStdin() so tests can wire a bytes.Buffer.
func confirmPrompt(cmd *cobra.Command, promptW io.Writer, prompt string) bool {
	fmt.Fprintf(promptW, "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		// Most common error: io.EOF from a piped empty stdin. Treat
		// as "no" so scripts that pipe /dev/null don't accidentally
		// delete a memory.
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// pluralisePtr renders a *string as the underlying value when set or
// the literal "(none)" when nil. Pulled into a helper because every
// summary printer in this package renders the same three optional
// fields (expires_at / user_sub / target_name) the same way; a
// shared helper keeps the table-render lines identical across
// remember / recall / list.
func pluralisePtr(p *string) string {
	if p == nil || *p == "" {
		return "(none)"
	}
	return *p
}

// writeBodyToStdout writes the entry body verbatim plus exactly one
// trailing newline. Same single-trailing-newline contract `meho kb
// show` uses — the substrate stores the body unmodified, so a
// hand-written memory that already ends in `\n` would otherwise
// produce two newlines when Fprintln adds its own.
func writeBodyToStdout(w io.Writer, body string) {
	fmt.Fprintln(w, strings.TrimRight(body, "\r\n"))
}
