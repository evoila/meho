// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package memory hosts the top-level cobra commands `meho remember`,
// `meho recall`, `meho forget`, `meho list`, and `meho promote` for
// G5.1-T4 (#424) + G5.2-T4 (#627) of Initiative #332. The five verbs
// wrap the REST routes shipped by G5.1-T2 (#422) plus the G0.4-T5
// `/api/v1/retrieve` route for the `meho recall --query` retrieval
// form:
//
//   - `meho remember "body" [--scope SCOPE] [--slug SLUG]
//     [--target NAME] [--tag T] [--ttl 7d] [--persist] [--json]` —
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
//   - `meho promote <scope>/<slug> --to <scope> [--move] [--json]` —
//     POST /api/v1/memory/{scope}/{slug}/promote (G5.2-T4 #626).
//
// The verbs are registered as **top-level** cobra commands per the
// consumer-needs.md §G5 ergonomic shape (the consumer-facing CLI
// spec calls out `meho remember` and `meho recall`, not
// `meho memory remember`). The acceptance criterion in #424 names
// `meho list --scope user` verbatim; `meho memory list` would
// violate the contract.
//
// G0.12-T10 #1268 migrated this package off the sibling-verb
// pattern of hand-rolled HTTP + hand-typed copies of backend
// pydantic models. Every verb here drives the generated
// `api.ClientWithResponses` surface directly: `api.NewAuthedClient`
// wires the bearer + lazy 401-refresh editor onto the embedded
// `ClientWithResponses`, and the verbs call the typed `*WithResponse`
// methods (`ListMemoriesApiV1MemoryGetWithResponse` etc.).
// Consumer-side struct drift — the #1069 root cause Initiative #1118
// targets — can't recur because we now consume `api.MemoryEntry`,
// `api.MemoryScope`, `api.MemoryListResponse`, `api.RetrievalHit`,
// and `api.RetrieveResponse` directly. The shared retrieval route's
// types live only in the generated client now (the parallel kb/
// migration in G0.12-T9 #1267 removed the matching duplicates from
// that sibling).
package memory

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// Scope is the local alias for the generated `api.MemoryScope` enum.
// Adopted in G0.12-T10 #1268 as the canonical typed-enum the package
// uses; the alias rather than a re-declaration lets every internal
// call site reference `Scope` (short, scoped to the verb tree)
// without the verb signatures or options structs leaking the
// generated `api.MemoryScope` name into the public surface. The wire-
// level identifier (route path segment, audit scope contextvar,
// `--scope` flag value) is whatever the underlying generated string
// carries.
//
// The alias matches the AC carve-out in #1268: "unless `Scope` stays
// because no typed-enum equivalent exists; if so, it gets a comment
// pointing at this Task." The typed enum DOES exist
// (`api.MemoryScope`); the alias *is* the adoption of it, not a
// duplicate.
type Scope = api.MemoryScope

// The five MemoryScope values exposed by the substrate, re-exported
// here so the verb runners + tests reference one package-local name
// per scope rather than the generated `api.MemoryScope*` family.
// Adopting the generated enum directly is the canonical move for
// G0.12 (the whole point of the migration is to delete consumer-side
// duplicates), but keeping the local aliases keeps the verb code
// scannable — `ScopeUserTenant` is shorter than
// `api.MemoryScopeUserTenant` everywhere it appears.
const (
	// ScopeUser is the per-operator-across-tenants scope — a memory
	// only the writing operator can read, across every tenant they
	// belong to.
	ScopeUser Scope = api.MemoryScopeUser
	// ScopeUserTenant is the operator-within-one-tenant scope — the
	// default for `meho remember` because consumer-needs.md §G5 L137
	// identifies it as the most common case.
	ScopeUserTenant Scope = api.MemoryScopeUserTenant
	// ScopeUserTarget is the operator-against-one-target scope —
	// requires `--target`.
	ScopeUserTarget Scope = api.MemoryScopeUserTarget
	// ScopeTenant is the tenant-wide scope — write requires
	// `tenant_admin`; the substrate's RBAC matrix surfaces this as
	// 403 from the service.
	ScopeTenant Scope = api.MemoryScopeTenant
	// ScopeTarget is the per-target-shared scope — every operator
	// touching one infrastructure target sees the same memory.
	// Requires `--target`.
	ScopeTarget Scope = api.MemoryScopeTarget
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

// errMissingAccessToken is the sentinel newAuthedClient returns
// when the stored token row exists but its access_token field is
// empty. Routed to auth_expired (exit 2) with a `meho login` hint
// rather than the generic transport-error path. Mirrors the kb
// sibling's fix that landed in PR #500 review iteration 1.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the memory verb tree's transport
// will read off any backplane response body. 1 MiB is generous: list
// pages cap at 500 rows server-side, and an average memory body is
// small (consumer-needs.md describes them as "useful behavioral
// preferences" not multi-KB blobs). Without the cap, an adversarial
// or runaway backplane response could OOM the CLI because the
// generated `Parse*Response` helpers call `io.ReadAll(rsp.Body)` on
// an unbounded body before constructing the typed envelope. The cap
// is installed at the transport layer via
// `api.AuthedClientOptions.ResponseBodyLimit` so it applies
// uniformly to every typed verb on the same `AuthedClient`.
const responseBodyCap int64 = 1 << 20

// loadBodyStdinCap bounds the `-` (stdin) read on `meho remember`
// so an adversarial / malformed pipe can't pin the verb in
// unbounded io.ReadAll. 1 MiB matches the response cap; memory
// bodies are expected to be hand-written notes, not megabyte
// blobs.
const loadBodyStdinCap int64 = 1 << 20

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded.
// Centralised so every verb's typed-call path goes through the
// same "stored-token-loaded + non-empty bearer" gate; the caller
// forwards any returned error to renderRequestError for category
// mapping. Mirrors the sibling verb-tree migrations
// (G0.12-T4 #1262, G0.12-T9 #1267). Opts into the transport-layer
// response-body cap (responseBodyCap) so the generated typed
// parsers can't be pinned by an unbounded backplane response.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{
		ResponseBodyLimit: responseBodyCap,
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
// 401, runs a one-shot bearer refresh and re-issues call. Same
// shape `kb` adopted in G0.12-T9 #1267 so every memory verb runs
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

// renderRequestError translates a transport-layer request error
// into the right output.StructuredError category. Maps the memory
// REST surface's pre-response failures: missing bearer, no-refresh-
// token, token-not-found, body-cap / parse failures bubbling out of
// the generated `*WithResponse` parsers, plus the generic transport-
// down case. Non-2xx status codes carried in a typed response
// envelope are classified by renderHTTPStatus instead.
//
// Parse / cap failures route to `output.Unexpected` (exit 4 —
// `unexpected_response`) rather than `output.Unreachable` (exit 3 —
// `network_unreachable`). A 1 MiB body cap firing or a JSON decode
// rejecting a malformed payload is a contract / shape failure on
// the server side, not a transport-down failure on the operator's
// side; surfacing it as "unreachable" would send operators chasing
// a network ghost. The cap is installed by `newAuthedClient` via
// `api.AuthedClientOptions.ResponseBodyLimit` (responseBodyCap).
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
	// Transport-layer body-cap firing (*http.MaxBytesReader returned
	// from capRoundTripper) and JSON shape failures bubbling out of
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
// value. Memory-route-specific notes:
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
