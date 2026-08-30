// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package automation hosts the cobra commands under `meho automation ...`
// for Task #3029 of Initiative #2900 (the add-on pairing contract). It ships
// one operator-facing verb that mirrors the `meho_automation_list` MCP
// meta-tool and the `GET /api/v1/automation` REST route:
//
//   - `meho automation list [--json]` — the paired-automation surface: the
//     add-on(s) advertising the `automation` meta-tool family, each with its
//     negotiated contract version, live contract-compatibility, liveness, and
//     declared surfaces (meta-tool / CLI verb families, console panels, event
//     kinds). Role: read_only.
//
// Gating — server-side only, CLI ↔ REST ↔ MCP parity (#2109 / #3029). The
// `meho automation` tree compiles into every CLI binary and is always visible;
// it carries no client-side pairing pre-check. Activation is decided by the
// backplane exactly as it is for `GET /api/v1/automation`: while no paired,
// contract-healthy add-on advertises the `automation` family the route answers
// 403 `automation_addon_not_active`, which the CLI renders as a clear
// "not active" message. The verb is a thin shell over the same route the REST
// surface exposes and the same activation view the meta-tool gate reads, so all
// three fronts give one verdict for one tenant.
//
// Like the sibling docs / kb verb trees, every call drives the generated
// `api.ClientWithResponses` surface directly: `api.NewAuthedClient` wires the
// bearer + lazy 401-refresh editor, and the verb consumes the generated
// `api.AutomationSurfaceResponse` / `api.AutomationProvider` types — no
// consumer-side copies of the backend pydantic models.
package automation

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho automation` parent command. The command tree
// compiles into every CLI binary and is always visible: there is no
// client-side pairing gate (#2109). Activation is decided server-side by the
// backplane, identically to `GET /api/v1/automation`.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "automation",
		Short: "Inspect the paired automation add-on surface",
		Long: "Operate the automation paired-add-on surface — the governed " +
			"automation product paired with this backplane (Initiative #2900). " +
			"The surface is active only while an automation add-on is paired " +
			"and contract-healthy; while it is not, every verb returns a typed " +
			"\"not active\" response rather than a silent or divergent " +
			"client-side refusal, mirroring the `meho_automation_list` meta-tool " +
			"and `GET /api/v1/automation`. Blueprint and workflow identifiers " +
			"are the add-on's own data, driven through the add-on, never CLI " +
			"verbs here.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	return cmd
}

// errMissingAccessToken mirrors the docs / kb verb trees' sentinel: the stored
// token row exists but its access_token field is empty. A credential-state
// failure (auth_expired, exit 2) rather than a transport failure.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the automation transport reads off any
// backplane response before surfacing `*http.MaxBytesError`. 1 MiB is generous:
// the automation surface is a small provider list (one row per paired add-on,
// each with a handful of advertised surfaces).
const responseBodyCap int64 = 1 << 20

// newAuthedClient builds an api.AuthedClient for the supplied backplane URL and
// verifies a non-empty bearer is loaded. Mirrors the docs verb tree's helper.
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

// retryOn401 invokes call once and, on a 401, runs a one-shot bearer refresh
// and re-issues call. Identical contract to the docs verb tree's helper.
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

// renderRequestError translates a transport-layer request error into the right
// output.StructuredError category — the same mapping the docs verb tree uses.
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

// renderHTTPStatus classifies a non-2xx automation response carried in the
// typed envelope. The status set `GET /api/v1/automation` can return:
//
//   - 401 → auth_expired (token rejected / refresh impossible).
//   - 403 → the surface is inactive (no paired, contract-healthy automation
//     add-on) when the detail marker is `automation_addon_not_active`;
//     otherwise a genuine role rejection rendered as insufficient_role.
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
		if decodeDetailString(bodyStr) == "automation_addon_not_active" {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(
					"no automation add-on is paired and contract-healthy; the "+
						"automation surface is inactive",
				),
				jsonOut,
			)
		}
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)),
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

// decodeDetailString pulls the `detail` field out of a FastAPI error body when
// it's a plain string, falling back to the trimmed raw body when the JSON shape
// doesn't match. Mirrors the docs verb tree's helper.
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

// truncate cuts s to maxLen runes, appending an ellipsis when truncation
// happened. Rune-aware so multi-byte UTF-8 survives the cut.
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
