// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package docs hosts the cobra commands under `meho docs ...` for
// G4.5-T5 (#1524) of Initiative #1518 (the meho-docs add-on), extended
// for collection-scoped search by G4.6-T3 (#1552). It ships one
// operator-facing verb that mirrors the `search_docs` MCP tool:
//
//   - `meho docs search <query> --collection <c> [--product <p>]
//     [--version <v>] [--limit N] [--json]` — collection-scoped
//     vendor-document retrieval via POST /api/v1/search_docs (T3,
//     #1552). Role: operator. --collection is the mandatory binary
//     scope (it routes to a backend and gates per-collection
//     entitlement); the CLI fails fast on a missing --collection
//     before the round-trip. --product / --version are optional
//     refinements within the collection.
//
// Gating — true-absence-when-unprovisioned (option B, the
// compiled-in fallback). The `meho docs` tree compiles into every
// CLI binary, but its visibility is gated on the tenant-provisioned
// `meho-docs` capability (T1, #1519). The capability is carried in
// the operator's JWT `capabilities` claim (the same claim the MCP
// registry filters tool visibility on). The CLI reads that claim
// from the stored bearer token at command-tree-build time and:
//
//   - shows `meho docs` in `meho --help` only when the tenant has
//     the `meho-docs` capability, and
//   - refuses any invocation with a typed, non-zero
//     "add-on not provisioned" error otherwise.
//
// Why decode the claim client-side rather than build a discovery
// route: the server-driven discovery channel
// (`discovery.Fetch` → GET /api/v1/commands) is anonymous by design
// (it never imports internal/api or internal/auth, and it fetches
// before login has produced a token), and its `Register` only grafts
// *stub* commands ("not yet implemented locally") — it cannot toggle
// the visibility of a real, compiled-in implementation per tenant. A
// tenant-filtered manifest would contradict the discovery channel's
// anonymous contract and require a new authenticated backend route +
// an OpenAPI snapshot regen. The client-side claim probe is purely a
// UX affordance: the real security boundary stays server-side (the
// route's RBAC plus the corpus federation), so a forged capability
// claim still cannot reach the corpus. Reading an *unverified* claim
// to decide whether to *show* a command is safe precisely because the
// gate never grants access on its own.
//
// Like the sibling kb verb tree (G0.12-T9 #1267), every call drives
// the generated `api.ClientWithResponses` surface directly:
// `api.NewAuthedClient` wires the bearer + lazy 401-refresh editor,
// and the verb consumes the generated `api.SearchDocsRequest`,
// `api.SearchDocsResponse`, and `api.DocsChunk` types — no
// consumer-side copies of the backend pydantic models.
package docs

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/output"
)

// Capability is the tenant-provisioned capability key that gates the
// `meho docs` tree. It is the same key the backend's
// `_extract_capabilities` surfaces on `Operator.capabilities` and
// the MCP registry filters `search_docs` visibility on (T1, #1519),
// kept as a single source of truth so the CLI gate and the server
// gate agree on the string.
const Capability = "meho-docs"

// NewRootCmd returns the `meho docs` parent command, gated on the
// `meho-docs` capability. The command tree compiles into every CLI
// binary; `provisioned` (resolved from the stored token's JWT
// capabilities claim) decides whether the parent is visible in
// `meho --help` and whether its verbs run or refuse.
//
// When the tenant lacks the capability the parent is marked Hidden
// and every leaf RunE short-circuits to a typed
// `addon_not_provisioned` error (exit 5, the same family as
// insufficient_role) before any network call. This makes the
// command genuinely non-runnable for an unprovisioned tenant rather
// than silently usable, while keeping the gate a UX affordance —
// the server still enforces the real boundary.
func NewRootCmd() *cobra.Command {
	provisioned := tenantHasDocsCapability()
	return newRootCmdWithGate(provisioned)
}

// newRootCmdWithGate builds the `meho docs` tree with the capability
// gate forced to a known value. Split out so tests can drive both the
// provisioned and unprovisioned shapes without seeding a token whose
// claim decodes to a specific capability set.
func newRootCmdWithGate(provisioned bool) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "docs",
		Short: "Search the meho-docs vendor-document add-on (provisioned tenants only)",
		Long: "Operate the meho-docs add-on — backplane-federated " +
			"retrieval over the external vendor-document corpus. The " +
			"add-on is a tenant-provisioned capability: when it is not " +
			"provisioned for your tenant, `meho docs` is hidden from " +
			"`meho --help` and every verb refuses with a typed " +
			"`addon_not_provisioned` error. Search routes through the " +
			"backplane so every query is audited and the mandatory " +
			"collection scope + per-collection entitlement are enforced " +
			"centrally.",
		// Hidden mirrors the absence the Initiative asks for: an
		// unprovisioned tenant should not see the command in --help.
		// The verb-level refusal (below) closes the "hidden but still
		// invokable" gap cobra's Hidden alone leaves open.
		Hidden:       !provisioned,
		SilenceUsage: true,
	}
	cmd.AddCommand(newSearchCmd(provisioned))
	return cmd
}

// errNotProvisioned is the typed refusal a docs verb returns when the
// tenant lacks the meho-docs capability. It surfaces as
// `addon_not_provisioned` (exit 5, the insufficient_role family) so
// scripts can distinguish "you can't use this add-on" from a missing
// flag (exit 4) or a transport failure (exit 3).
func errNotProvisioned(cmd *cobra.Command, jsonOut bool) error {
	return output.RenderError(
		cmd.ErrOrStderr(),
		&output.StructuredError{
			Code: "addon_not_provisioned",
			Detail: "the meho-docs add-on is not provisioned for your " +
				"tenant; ask a tenant_admin to enable the `meho-docs` " +
				"capability",
			Exit: output.ExitInsufficientRole,
		},
		jsonOut,
	)
}

// tenantHasDocsCapability reports whether the operator's stored token
// carries the meho-docs capability in its JWT `capabilities` claim.
//
// Fail-closed: any failure to resolve or decode the token (no login,
// unreadable store, malformed JWT, absent/garbage claim) returns
// false, so the command stays hidden + refusing rather than visible.
// This mirrors the backend's fail-closed `_extract_capabilities`
// posture (T1, #1519): a missing/garbage capability claim must never
// be read as a grant.
//
// The claim is read **unverified** — the CLI does not hold the realm
// signing key and does not need it. This is a visibility affordance,
// not a security check; the backplane re-validates the JWT and the
// corpus federation enforces the real boundary on every call.
func tenantHasDocsCapability() bool {
	cfg, err := auth.LoadConfig()
	if err != nil || cfg.BackplaneURL == "" {
		return false
	}
	tok, err := loadStoredToken(cfg.BackplaneURL)
	if err != nil || tok.AccessToken == "" {
		return false
	}
	caps, err := capabilitiesFromJWT(tok.AccessToken)
	if err != nil {
		return false
	}
	_, ok := caps[Capability]
	return ok
}

// loadStoredToken is the token-load seam — split out so docs_test.go
// can stub it deterministically (the alternative, seeding a keyring /
// file store with a token whose JWT decodes to a specific capability
// set, is exercised in an end-to-end test, but the seam keeps the
// gate's claim-decode logic unit-testable in isolation).
var loadStoredToken = func(backplaneURL string) (auth.StoredToken, error) {
	store, err := auth.NewTokenStore()
	if err != nil {
		return auth.StoredToken{}, err
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	return store.Load(service, user)
}

// capabilitiesFromJWT decodes the `capabilities` claim out of a JWT's
// payload segment and returns it as a set. The token is parsed
// structurally (split on '.', base64url-decode the payload) — the
// signature is **not** verified, by design (see
// tenantHasDocsCapability). Returns an error on any structural
// failure so the caller can fail closed.
//
// The claim is read as a JSON array of strings (the shape the
// backend's `_extract_capabilities` accepts); a claim that is absent,
// null, or not an array yields the empty set (no error) so a token
// minted before the capability mapper existed simply sees no
// capabilities rather than erroring the whole gate.
func capabilitiesFromJWT(token string) (map[string]struct{}, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("meho: token is not a JWT (expected 3 dot-separated segments)")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("meho: decode JWT payload: %w", err)
	}
	var claims struct {
		Capabilities []string `json:"capabilities"`
	}
	if err := json.Unmarshal(payload, &claims); err != nil {
		return nil, fmt.Errorf("meho: parse JWT claims: %w", err)
	}
	set := make(map[string]struct{}, len(claims.Capabilities))
	for _, c := range claims.Capabilities {
		if c != "" {
			set[c] = struct{}{}
		}
	}
	return set, nil
}

// errMissingAccessToken mirrors the kb verb tree's sentinel: the
// stored token row exists but its access_token field is empty. A
// credential-state failure (auth_expired, exit 2) rather than a
// transport failure (unreachable, exit 3).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the docs transport reads off any
// backplane response before surfacing `*http.MaxBytesError`. 1 MiB is
// generous for a search_docs response: the backend caps `limit` at 50
// and each DocsChunk is a single corpus chunk (content + citation +
// score). Without the cap an adversarial or runaway backplane could
// OOM the CLI because the generated `Parse*Response` helpers call
// `io.ReadAll(rsp.Body)` before constructing the typed envelope.
const responseBodyCap int64 = 1 << 20

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Mirrors
// the kb verb tree's helper (same stored-token + non-empty-bearer
// gate, same transport-layer response-body cap) so the docs verbs
// share one auth path.
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

// retryOn401 invokes call once and, on a 401, runs a one-shot bearer
// refresh and re-issues call. Identical contract to the kb verb
// tree's helper, generalised over the typed response envelope.
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
// the right output.StructuredError category. Same mapping the kb
// verb tree uses: missing bearer / no-refresh-token / token-not-found
// route to auth_expired (exit 2); body-cap and JSON-shape failures
// route to unexpected_response (exit 4, a server-side contract
// failure, not a transport-down failure); everything else is
// unreachable (exit 3).
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

// renderHTTPStatus classifies a non-2xx search_docs response carried
// in the typed envelope. The status set search_docs can return (per
// the T3 route contract):
//
//   - 401 → auth_expired (token rejected / refresh impossible).
//   - 403 → insufficient_role (read_only operator, OR the tenant is not
//     entitled to the named collection — it lacks the
//     `meho-docs:<collection>` capability).
//   - 409 → unexpected wrapping the not-ready detail (the collection is
//     known + entitled but its backend is provisioning / rebuilding /
//     disabled).
//   - 422 → unexpected wrapping the collection-scope detail (missing
//     --collection — the CLI fails fast before the call, so a 422 here
//     means an unknown collection or a contract drift worth surfacing)
//     or a too-long query / out-of-range limit.
//   - 503 → unexpected with the backend-unavailable detail (the
//     collection's backend is unconfigured / unreachable / non-2xx).
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
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", decodeDetailString(bodyStr))),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"the doc collection is not ready: %s",
				decodeDetailString(bodyStr),
			)),
			jsonOut,
		)
	case http.StatusServiceUnavailable:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"the collection's backend is unavailable: %s",
				decodeDetailString(bodyStr),
			)),
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
// body when it's a plain string, falling back to the trimmed raw body
// when the JSON shape doesn't match (non-JSON body or a structured
// `detail` such as the FastAPI validation list). Mirrors the kb verb
// tree's helper.
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
// cut. Same shape as the sibling-package helpers.
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
