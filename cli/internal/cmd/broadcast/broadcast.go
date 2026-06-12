// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package broadcast hosts the cobra commands under `meho broadcast ...`
// for G6.3-T4 (#381) of Initiative #376. v0.2 ships three tenant-admin
// verbs wrapping the T4 REST surface (`/api/v1/broadcast/overrides`):
//
//   - `meho broadcast overrides list [--op-id-pattern P] [--json]
//     [--backplane URL]` — GET the operator's tenant's rules.
//   - `meho broadcast overrides set --op-id-pattern P [--scope-field F
//     --scope-value V] --detail D [--json] [--backplane URL]` — POST
//     a new rule.
//   - `meho broadcast overrides remove <id> [--json] [--backplane URL]` —
//     DELETE one rule by id.
//
// Each verb authenticates via the token `meho login` wrote, same
// shape as `meho audit` / `meho targets` / `meho retrieval`. RBAC at
// the backend rejects non-`tenant_admin` callers with HTTP 403; the
// verb renders this as `insufficient_role`.
//
// G0.12-T6 #1264 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of the backend Pydantic
// models. Every verb here drives the generated
// `api.ClientWithResponses` surface directly: `api.NewAuthedClient`
// wires the bearer + lazy 401-refresh editor onto the embedded
// `ClientWithResponses`, and the verbs call the typed
// `*WithResponse` methods
// (`ListOverridesApiV1BroadcastOverridesGetWithResponse`,
// `CreateOverrideApiV1BroadcastOverridesPostWithResponse`,
// `DeleteOverrideApiV1BroadcastOverridesOverrideIdDeleteWithResponse`).
// Consumer-side struct drift — the #1069 root cause — can't recur
// because we now consume `api.BroadcastOverrideRead` and produce
// `api.BroadcastOverrideCreate` directly.
package broadcast

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho broadcast` parent command. Mounted on
// the top-level meho tree by cmd/root.go alongside `meho audit`,
// `meho operation`, etc.
//
// The parent command takes no args and prints its own help; every
// behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "broadcast",
		Short:        "Manage broadcast-detail overrides (overrides list / set / remove)",
		Long:         broadcastLongHelp,
		SilenceUsage: true,
	}
	cmd.AddCommand(newOverridesCmd())
	return cmd
}

const broadcastLongHelp = "Tenant-admin verbs for managing per-tenant broadcast-detail " +
	"override rules. The rules feed the publish-time resolver (G6.3-T2 #379) " +
	"that decides whether each broadcast event renders full-detail or " +
	"aggregate-only. Every verb is tenant_admin-only; non-admin callers " +
	"see 403 insufficient_role. Cross-tenant probes (DELETE on another " +
	"tenant's id) return 404 -- existence is not leaked across tenant " +
	"boundaries."

// newOverridesCmd returns the `meho broadcast overrides` parent.
func newOverridesCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "overrides",
		Short: "List, create, and delete broadcast-detail override rules",
		Long: "overrides wraps the three /api/v1/broadcast/overrides routes " +
			"shipped by G6.3-T4 (#381). Use `list` to inspect, `set` to " +
			"create a new rule, and `remove` to delete one by id.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newOverridesListCmd())
	cmd.AddCommand(newOverridesSetCmd())
	cmd.AddCommand(newOverridesRemoveCmd())
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

// routeRequestError is the single dispatcher every verb feeds an
// error from `listOverrides` / `createOverride` / `deleteOverride`
// into. The error is either an `*httpResponseError` (the backplane
// responded with a non-2xx status) or a transport-layer failure
// (network, refresh-impossible, etc.); we route the former through
// `renderHTTPStatus` and the latter through `renderTransportError`.
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

// renderHTTPStatus maps a non-2xx HTTP status from the typed-client
// response onto the right structured-error category. Mirrors the
// pre-migration `renderHTTPError` shape:
//
//   - 401 → AuthExpired
//   - 403 → InsufficientRole(detail) -- the backend's own detail
//     string surfaces, not a hardcoded message.
//   - 404 → Unexpected(detail) -- list/set on an older backplane
//     (route missing) and remove on a missing/cross-tenant id both
//     hit this path; the backend's own detail
//     (`broadcast_override_not_found` for remove) round-trips
//     cleanly.
//   - 409 → Unexpected(detail) -- duplicate-rule rejection.
//   - 422 → Unexpected("invalid request: <body>")
//   - other non-2xx → Unexpected("call <url>: HTTP N: <body>")
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
			output.InsufficientRole(decodeDetail(body, bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetail(body, bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetail(body, bodyStr)),
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

// decodeDetail extracts the FastAPI `{"detail": "..."}` envelope's
// string payload from a response body, falling back to the trimmed
// body when the envelope can't be decoded. Pre-migration this lived
// in `decodeDetailString`; the typed-client error path still receives
// raw bytes so the same shape applies. `fallback` is the
// already-trimmed body so the empty-body placeholder ("(empty body)")
// flows through unchanged. FastAPI's `detail` can be either a
// string (HTTPException) or a nested object (422-style validation
// envelope); the string-shape case is the operator-friendly one we
// surface, anything else gets the raw-body fallback.
func decodeDetail(body []byte, fallback string) string {
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(body, &env); err == nil {
		var s string
		if jerr := json.Unmarshal(env.Detail, &s); jerr == nil && s != "" {
			return s
		}
	}
	return fallback
}
