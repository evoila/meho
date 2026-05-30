// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package runbook hosts the cobra commands under `meho runbook ...`
// for G12.5-T1 (#1318) of Initiative #1200. T1 ships the chassis
// (root cobra command, output formatting, error rendering, YAML
// parsing for the import verbs) plus the six template verbs that
// wrap the G12.2 REST surface (#1297):
//
//   - `meho runbook list-templates [--status S] [--target-kind K]
//     [--limit N] [--json]` — list templates in the operator's tenant
//     via GET /api/v1/runbooks/templates. Role: operator.
//   - `meho runbook show-template <slug> [--version N] [--json]` —
//     fetch a single template (full step body) via GET
//     /api/v1/runbooks/templates/{slug}. Role: tenant_admin (with the
//     post-completion carve-out implemented backend-side per #1309).
//   - `meho runbook draft-template <slug> --from <file.yaml>
//     [--json]` — create the first draft of a new slug via POST
//     /api/v1/runbooks/templates. Role: tenant_admin.
//   - `meho runbook edit-template <slug> --from <file.yaml>
//     [--json]` — edit the current draft (in place) or fork a
//     published version into a new draft via PATCH
//     /api/v1/runbooks/templates/{slug}. Role: tenant_admin.
//   - `meho runbook publish-template <slug> --version N [--json]` —
//     flip a draft to published via POST
//     /api/v1/runbooks/templates/{slug}/publish. Role: tenant_admin.
//   - `meho runbook deprecate-template <slug> --version N [--json]` —
//     mark a published version as deprecated via POST
//     /api/v1/runbooks/templates/{slug}/deprecate. Role: tenant_admin.
//
// G12.5-T2 (#1319) extends the same parent with the five run verbs
// (start / next / abort / reassign / runs); G12.5-T3 (#1320) ships
// the operator-facing CLI docs.
//
// Every verb wraps one G12.2 route and drives the generated
// `api.ClientWithResponses` directly via `api.NewAuthedClient`. The
// helper trio (`newAuthedClient` / `retryOn401` / `renderHTTPStatus`)
// mirrors the kb / memory / conventions packages — duplicated by
// design (avoids cyclic imports between sibling cobra packages),
// same shape across the codebase.
//
// The two non-trivial verbs are `draft-template` and `edit-template`:
// both accept `--from <file.yaml>`, parse the YAML into a struct
// matching the backend's `RunbookTemplateBody` shape, run a local
// pre-flight (slug regex, step-id uniqueness, step / verify type
// allowlists, substitution allowlist), and only then POST/PATCH.
// The pre-flight is a UX layer, not a security boundary — the
// backend re-validates authoritatively (see
// `backend/src/meho_backplane/runbooks/schemas.py`'s
// `_validate_step_ids_unique_and_substitutions_allowlisted` and
// `validate_substitutions`).
package runbook

import (
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

// NewRootCmd returns the `meho runbook` parent command. The command
// is grafted onto the top-level meho command tree by cmd/root.go
// alongside `meho kb`, `meho memory`, and the other v0.2 verb trees.
// The parent itself takes no args and prints its own help; every
// piece of behaviour lives in the per-subcommand RunE closures.
//
// T1 (#1318) registers six template verbs; T2 (#1319) extends the
// parent with five run verbs (start / next / abort / reassign /
// runs) in a follow-up PR.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "runbook",
		Short: "Author and operate runbook templates (list/show/draft/edit/publish/deprecate)",
		Long: "Operate runbook templates from the operator shell. " +
			"Authors (tenant_admin) draft, edit, publish, and deprecate " +
			"templates; operators list-templates and (post-completion) " +
			"show-template after a run finishes. Read verbs are " +
			"operator-level by default; write verbs and unconditional " +
			"show-template require tenant_admin. Tenant scoping is " +
			"enforced server-side via the JWT.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListTemplatesCmd())
	cmd.AddCommand(newShowTemplateCmd())
	cmd.AddCommand(newDraftTemplateCmd())
	cmd.AddCommand(newEditTemplateCmd())
	cmd.AddCommand(newPublishTemplateCmd())
	cmd.AddCommand(newDeprecateTemplateCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its `access_token` field is empty.
// Credential-state failure → renderRequestError maps it to
// auth_expired (exit 2) with a `meho login` hint. Same shape as the
// sibling typed-client migrations (kb, memory, conventions).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the runbook verb tree's transport
// will read off any backplane response body before surfacing
// `*http.MaxBytesError`. 1 MiB is generous for every documented
// runbook template payload — list pages cap at the backend's
// `Query(le=500)` limit × ~256-byte summary rows; show returns one
// template whose `steps` JSONB is bounded by author convention but
// has no documented hard cap (audited median ~8 KB). Without the
// cap, an adversarial or runaway backplane response could OOM the
// CLI because the generated `Parse*Response` helpers call
// `io.ReadAll(rsp.Body)` on an unbounded body. The cap is installed
// at the transport layer via `api.AuthedClientOptions.ResponseBodyLimit`
// so it applies uniformly to every typed verb on the same
// `AuthedClient`. Same value the kb / conventions packages adopted.
const responseBodyCap int64 = 1 << 20

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Centralised
// so every verb's typed-call path goes through the same
// "stored-token-loaded + non-empty bearer" gate. Opts into the
// transport-layer response-body cap so the generated typed parsers
// can't be pinned by an unbounded backplane response.
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
// 401, runs a one-shot bearer refresh and re-issues call. Mirrors
// the behaviour `api.AuthedClient.GetHealth` implements, generalised
// so every runbook verb runs the same transparent-retry contract.
//
// statusOf reads the StatusCode off the typed response envelope (the
// generated *Response types expose StatusCode() through their embedded
// *http.Response). A nil response counts as "no retry" — the
// transport already failed and the caller surfaces err directly.
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
// verb tree uses — credential-state failures route to auth_expired
// (exit 2), parse / cap failures route to unexpected_response (exit
// 4), everything else falls through to unreachable (exit 3).
//
// Parse / cap failures route to `output.Unexpected` (exit 4) rather
// than `output.Unreachable` (exit 3): a 1 MiB body cap firing or a
// JSON decode rejecting a malformed payload is a contract / shape
// failure on the server side, not a transport-down failure on the
// operator's side.
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

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. The mapping mirrors the kb tree's
// renderHTTPStatus with template-specific 4xx surfaces:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role with the backend's detail string.
//     The backend renders `Insufficient role: tenant_admin required`
//     for the write verbs and `Insufficient role: tenant_admin
//     required (post-completion exception not satisfied)` for the
//     show-template post-completion carve-out (#1309).
//   - 400 → unexpected with the backend's detail (e.g. publish on
//     a deprecated version → `cannot publish a deprecated version`).
//   - 404 → unexpected with the backend's detail (e.g.
//     `slug_not_found`; cross-tenant probes land here per the
//     no-existence-leak posture).
//   - 409 → unexpected with the backend's detail (e.g.
//     `draft_already_exists` from POST against an existing draft).
//   - 422 → unexpected wrapping the FastAPI validation envelope
//     (invalid slug, malformed step body, disallowed substitution
//     pattern). The backend's detail array carries `loc` for the
//     offending YAML / JSON path; preserving the envelope lets
//     operators paste it into the issue when reporting.
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
		// The role error message is operator-readable and load-bearing
		// — the issue body's AC pins "This verb requires TENANT_ADMIN
		// role" rendering on 403. The backend already emits that exact
		// shape ("Insufficient role: tenant_admin required"); we
		// surface its detail verbatim rather than re-translating so
		// the wording stays in lock-step with the backend's surface
		// (and with the post-completion carve-out variant for
		// show-template).
		return output.RenderError(cmd.ErrOrStderr(),
			output.InsufficientRole(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusBadRequest:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// The substrate returns `slug_not_found` for genuine absence
		// and cross-tenant probes alike — the conflation prevents
		// leaking tenant boundaries via status-code differential.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusConflict:
		// G12.2's draft / publish surface uses 409 for state-machine
		// rejections (draft already exists, version not in the
		// expected state). Surface the detail verbatim so the
		// operator sees which transition failed.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		// 422 from invalid slug shape, malformed step body, or a
		// disallowed substitution pattern. The backend emits the
		// FastAPI validation envelope; preserving the body lets the
		// operator see the `loc` path of the offending field.
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
// body when it's a plain string. Falls back to the trimmed raw body
// when the JSON shape doesn't match (non-JSON body or `detail` is a
// structured value such as the FastAPI validation list).
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

// pathEscape escapes a single path segment for use inside a backend
// URL. The generated typed client's path-parameter encoding uses the
// same `url.PathEscape` rule under the hood; we export the helper for
// tests that pin operator-typical slug shapes (dots, hyphens) survive
// encoding intact.
func pathEscape(segment string) string {
	return url.PathEscape(segment)
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
