// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package kb hosts the cobra commands under `meho kb ...` for
// G4.1-T4 (#418) of Initiative #331. v0.2 ships six operator-facing
// verbs that wrap the T2 REST surface (#416) shipped by
// `backend/src/meho_backplane/api/v1/kb.py` plus the G0.4-T5
// `/api/v1/retrieve` route for the search verb:
//
//   - `meho kb ingest <directory> [--dry-run] [--json]` — server-side
//     bulk ingest via POST /api/v1/kb/ingest. Role: tenant_admin.
//   - `meho kb search <query> [--limit N] [--json]` — kb-scoped
//     hybrid retrieval via POST /api/v1/retrieve with source="kb".
//     Role: operator.
//   - `meho kb list [--filter P] [--limit N] [--offset N] [--json]` —
//     paginated entry listing via GET /api/v1/kb. Role: operator.
//   - `meho kb show <slug> [--json]` — single-entry fetch via
//     GET /api/v1/kb/{slug}. Role: operator. Renders the body as
//     plain Markdown unless --json is set.
//   - `meho kb add <slug> --body @file.md [--metadata k=v,...]
//     [--json]` — single-entry create/re-index via POST /api/v1/kb.
//     Role: tenant_admin. The --body flag accepts inline text,
//     @<path> file references, and @- for stdin.
//   - `meho kb delete <slug> [--confirm] [--json]` — single-entry
//     deletion via DELETE /api/v1/kb/{slug}. Role: tenant_admin.
//     Prompts for confirmation on stdin unless --confirm is set;
//     idempotent (delete-already-missing returns 204 from the
//     backend).
//
// Each verb wraps one backplane route and renders the response
// either as a human-readable form (text table, Markdown body,
// key-value summary) or as `--json` mode. Authentication piggybacks
// on the token meho login wrote — same pattern as `meho audit`,
// `meho targets`, and `meho connector`.
//
// G0.12-T9 #1267 migrated this package off the sibling-verb pattern
// of hand-rolled HTTP + hand-typed copies of backend pydantic models.
// Every verb here drives the generated `api.ClientWithResponses`
// surface directly: `api.NewAuthedClient` wires the bearer + lazy
// 401-refresh editor onto the embedded `ClientWithResponses`, and
// the verbs call the typed `*WithResponse` methods
// (`ListKbApiV1KbGetWithResponse` etc.). Consumer-side struct drift
// — the #1069 root cause Initiative #1118 targets — can't recur
// because we now consume `api.KbEntry`, `api.KbEntryPreview`,
// `api.KbListResponse`, `api.KbIngestionResult`, `api.RetrievalHit`,
// and `api.RetrieveResponse` directly. The shared retrieval route's
// types live only in the generated client now (the parallel
// memory/ migration to land in G0.12-T10 removes its sibling
// duplicates).
package kb

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho kb` parent command. The command is
// grafted onto the top-level meho command tree by cmd/root.go
// alongside `meho audit`, `meho targets`, `meho connector`,
// `meho operation`, and `meho retrieval`. The parent itself takes
// no args and prints its own help; every piece of behaviour lives
// in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "kb",
		Short: "Operate the MEHO knowledge base (ingest / search / list / show / add / delete)",
		Long: "Operate the tenant-scoped knowledge base wired by G4.1. " +
			"Ingest, search, list, show, add, and delete kb entries. " +
			"Write verbs (ingest / add / delete) require tenant_admin; " +
			"read verbs are operator-level. Tenant scoping is enforced " +
			"server-side via the JWT — no surface accepts a tenant id.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIngestCmd())
	cmd.AddCommand(newSearchCmd())
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newAddCmd())
	cmd.AddCommand(newDeleteCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when
// the stored token row exists but its `access_token` field is
// empty. It's a credential-state failure rather than a transport
// failure, so renderRequestError maps it to auth_expired (exit 2)
// with a `meho login` hint — not unreachable (exit 3). Mirrors the
// shape adopted by the sibling typed-client migrations on Initiative
// #1118 (T1 #1251 approvals, T4 #1262 agent-principal).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// newAuthedClient builds an api.AuthedClient for the supplied
// backplane URL and verifies a non-empty bearer is loaded. Centralised
// so every verb's typed-call path goes through the same
// "stored-token-loaded + non-empty bearer" gate; the caller forwards
// any returned error to renderRequestError for category mapping.
// Mirrors the helper sibling verb-tree migrations (G0.12-T4 #1262)
// adopted for the same reason.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{})
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
// /api/v1/health endpoint, generalised so every kb verb runs the
// same transparent-retry contract.
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
// the right output.StructuredError category. Maps the kb REST
// surface's pre-response failures: missing bearer, no-refresh-
// token, token-not-found, plus the generic transport-down case.
// Non-2xx status codes carried in a typed response envelope are
// classified by renderHTTPStatus instead.
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
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// renderHTTPStatus classifies a non-2xx response (or 401 after a
// failed refresh) carried in the typed envelope into the right
// StructuredError category. Mirrors the pre-migration `renderHTTPError`
// switch but acts on the (statusCode, body) pair lifted off the
// generated `*Response.HTTPResponse` + `Body` fields rather than a
// sentinel value. The mapping preserved across the migration:
//
//   - 401 → auth_expired (refresh impossible / token rejected).
//   - 403 → insufficient_role with the backend's detail string.
//   - 400 → unexpected with the backend's detail (kb.ingest
//     directory_not_found / not_a_directory).
//   - 404 → unexpected with the backend's detail (slug_not_found;
//     cross-tenant probes land here per the no-existence-leak
//     posture).
//   - 422 → unexpected wrapping the FastAPI validation envelope
//     (invalid slug, missing required body field, both-or-neither
//     directory/tarball_url).
//   - 501 → unexpected with the backend's detail (tarball_url ingest
//     unsupported in v0.2).
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
	case http.StatusBadRequest:
		// Ingest 400 = directory_not_found / not_a_directory from the
		// backplane substrate. Surface the detail verbatim so the
		// operator sees the path the backplane failed to read.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusNotFound:
		// `meho kb show <slug>` and `meho kb delete <slug>` on an
		// absent slug surface here. The backend returns
		// `slug_not_found` for both genuine absence and cross-tenant
		// probes — the conflation prevents leaking tenant boundaries
		// via status-code differential. Note that delete itself is
		// idempotent (204 on missing) per the kb route contract; a
		// 404 from delete therefore signals a contract drift worth
		// surfacing rather than swallowing.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		// 422 from invalid slug shape, missing required body field,
		// or the exactly-one-of directory/tarball_url contract. The
		// backend emits the FastAPI validation envelope.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", bodyStr)),
			jsonOut,
		)
	case http.StatusNotImplemented:
		// 501 from kb.ingest when the operator supplied
		// `tarball_url`. The substrate exposes only ingest_directory
		// in v0.2; the route surfaces a clear "not implemented" so
		// callers don't silently lose work.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
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
// URL. Retained because tests pin the operator-typical slug shapes
// (dots, hyphens) survive escaping intact; the typed-client's
// `ShowKbApiV1KbSlugGetWithResponse` path-parameter encoding uses
// the same `url.PathEscape` rule under the hood.
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

// loadBodyFlag reads a flag value supporting inline text, `@<path>`
// for file references, and `@-` for stdin. Returns the loaded body
// or an error. The substrate's `body` field has a `min_length=1`
// constraint, so an empty result is rejected at the CLI rather than
// after a 422 round-trip.
//
// `@-` reads from cmd.InOrStdin() so tests can wire a bytes.Buffer;
// the read is bounded by `loadBodyStdinCap` to protect against an
// adversarial pipe (a runaway `tail -f` redirect, etc.).
//
// Trailing newlines (LF or CRLF) are stripped from file/stdin reads
// so a 1-line file passed via `@` doesn't carry a gratuitous
// trailing newline through the JSON body. Embedded newlines inside
// the text are preserved; only the final CRLF / LF is stripped.
// Inline text values pass through verbatim.
func loadBodyFlag(cmd *cobra.Command, raw string) (string, error) {
	if raw == "" {
		return "", errors.New("--body is required (inline text, @<path>, or @-)")
	}
	if !strings.HasPrefix(raw, "@") {
		return raw, nil
	}
	path := strings.TrimPrefix(raw, "@")
	if path == "-" {
		blob, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), loadBodyStdinCap+1))
		if err != nil {
			return "", fmt.Errorf("read --body from stdin: %w", err)
		}
		if int64(len(blob)) > loadBodyStdinCap {
			return "", fmt.Errorf(
				"--body from stdin exceeds %d-byte cap; pass a file with @<path> instead",
				loadBodyStdinCap,
			)
		}
		out := strings.TrimRight(string(blob), "\r\n")
		if out == "" {
			return "", errors.New("--body @- read empty input; cannot create kb entry with empty body")
		}
		return out, nil
	}
	blob, err := readBodyFile(path)
	if err != nil {
		return "", err
	}
	out := strings.TrimRight(string(blob), "\r\n")
	if out == "" {
		return "", fmt.Errorf("--body file %q is empty; cannot create kb entry with empty body", path)
	}
	return out, nil
}

// loadBodyStdinCap bounds the @- read so an adversarial / malformed
// pipe can't pin a kb add verb in unbounded ReadAll. 1 MiB is
// generous for any realistic Markdown entry (the consumer's longest
// entries are ~10 KB).
const loadBodyStdinCap int64 = 1 << 20

// readBodyFile is the file-read seam — split out so kb_test.go can
// stub it (testing a real os.ReadFile path through a temp file is
// fine, but the seam exists for cases where the underlying
// filesystem error must be deterministic).
var readBodyFile = func(path string) ([]byte, error) {
	blob, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read --body file %q: %w", path, err)
	}
	return blob, nil
}

// parseMetadataFlag parses the --metadata "k=v,k=v" flag value into a
// map. Returns nil for an empty value so the caller can omit the
// "metadata" key from the JSON body (the backend defaults to {}
// when the field is absent). Trims whitespace around keys + values
// so `--metadata "k = v, x = y"` works. Keys must be non-empty;
// values may be empty.
//
// Comma + equals are not escapable in v0.2 — operators who need
// commas or equals inside a value should construct the JSON body
// directly via the REST API or wait for v0.2.next. The CLI surface
// is the convenience path; the full key/value-space lives behind
// the REST route.
func parseMetadataFlag(raw string) (map[string]any, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	out := make(map[string]any)
	for _, pair := range strings.Split(raw, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		key, value, ok := strings.Cut(pair, "=")
		if !ok {
			return nil, fmt.Errorf("--metadata pair %q is missing '=' separator", pair)
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if key == "" {
			return nil, fmt.Errorf("--metadata pair %q has empty key", pair)
		}
		out[key] = value
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// confirmPrompt prompts on stdin/stdout with the given message and
// returns true only when the operator types y/yes/Y. EOF (closed
// stdin) is treated as a no — scripted use must pass --confirm.
// Honours cmd.InOrStdin() so tests can wire a bytes.Buffer.
// Mirrors `confirm` in cli/internal/cmd/connector/connector.go.
func confirmPrompt(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.OutOrStdout(), "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		// Most common error: io.EOF from a piped empty stdin.
		// Treat as "no" so scripts that pipe /dev/null don't
		// accidentally delete a kb entry.
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}
