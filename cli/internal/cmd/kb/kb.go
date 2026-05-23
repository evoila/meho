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
// The implementation deliberately follows the in-package HTTP helper
// pattern the sibling verb trees use (one resolveBackplane /
// doAuthedRequest / renderRequestError trio per package) rather
// than a shared cli/internal/api_client package. The reason is the
// import-cycle one: each verb tree is registered onto the root
// command, so a shared helper imported from cmd/* and from any
// per-tree package would close the cycle. Duplicating the helpers
// (a handful of small, stable functions) is the convention every
// sibling package follows.
package kb

import (
	"bytes"
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

// EntryPreview mirrors the backend KbEntryPreview pydantic model
// (`backend/src/meho_backplane/api/v1/kb.py`). Hand-written rather
// than aliased to the generated client type so the kb package stays
// decoupled from oapi-codegen churn — the targets / retrieval /
// audit / connector / operation packages take the same stance.
// Unprefixed name follows the sibling-package convention
// (`audit.Entry`, `connector.IngestionResult`, etc.) and avoids the
// `revive` stutter rule (`kb.KbEntryPreview` → `kb.EntryPreview`).
type EntryPreview struct {
	Slug      string         `json:"slug"`
	Preview   string         `json:"preview"`
	Metadata  map[string]any `json:"metadata"`
	CreatedAt string         `json:"created_at"`
	UpdatedAt string         `json:"updated_at"`
}

// ListResponse mirrors the backend KbListResponse envelope. Wrapped
// in `{"entries": [...]}` for forward-compat with future paging
// fields — same shape connectors_ingest adopted for its list
// response.
type ListResponse struct {
	Entries []EntryPreview `json:"entries"`
}

// Entry mirrors the backend KbEntry pydantic model — the full entry
// shape returned by GET /api/v1/kb/{slug} and POST /api/v1/kb.
// `Metadata` decodes as `map[string]any` so the CLI can re-emit it
// in --json mode without re-typing every Markdown front-matter shape
// the operator might store.
type Entry struct {
	ID        string         `json:"id"`
	TenantID  string         `json:"tenant_id"`
	Slug      string         `json:"slug"`
	Body      string         `json:"body"`
	Metadata  map[string]any `json:"metadata"`
	CreatedAt string         `json:"created_at"`
	UpdatedAt string         `json:"updated_at"`
}

// IngestionResult mirrors the backend KbIngestionResult shape.
// Counters partition every discovered .md file into exactly one of
// four buckets (inserted / updated / skipped / error); the substrate
// invariant is `inserted + updated + skipped + error == total .md
// files`. `Errors` carries per-file error strings (path + reason).
type IngestionResult struct {
	InsertedCount int      `json:"inserted_count"`
	UpdatedCount  int      `json:"updated_count"`
	SkippedCount  int      `json:"skipped_count"`
	ErrorCount    int      `json:"error_count"`
	Errors        []string `json:"errors"`
}

// RetrievalHit mirrors the backend RetrievalHit pydantic model
// (`backend/src/meho_backplane/retrieval/retriever.py`). Returned by
// POST /api/v1/retrieve. `BM25Score`, `CosineScore`, `BM25Rank`, and
// `CosineRank` are `*float64` / `*int` because the backend emits
// `null` for documents that did not appear in that signal's top-K
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

// RetrieveResponse mirrors the backend RetrieveResponse envelope.
type RetrieveResponse struct {
	Hits            []RetrievalHit `json:"hits"`
	QueryDurationMS float64        `json:"query_duration_ms"`
}

// errMissingAccessToken is the sentinel doAuthedRequest returns
// when the stored token row exists but its `access_token` field is
// empty. It's a credential-state failure rather than a transport
// failure, so renderRequestError maps it to auth_expired (exit 2)
// with a `meho login` hint — not unreachable (exit 3). Pre-existing
// sibling packages (audit/, targets/, etc.) emit a generic error
// here that falls through to unreachable; the local fix here is
// scoped to the kb package (m3 in the review punch list).
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// renderRequestError translates an error from one of the per-verb
// request helpers into the right output.StructuredError category.
// Same classification ladder as the audit / targets siblings with
// kb-specific 4xx handling:
//
//   - empty stored bearer → auth_expired (the row exists but its
//     access_token is empty, so the right fix is `meho login` even
//     though there's no transport-level 401 yet).
//   - 401 (refresh failed) → auth_expired with a `meho login` hint.
//   - 403 (RBAC denial) → insufficient_role; the backend's 403 detail
//     names the required role.
//   - 404 → unexpected with the backend's detail (kb routes return
//     `slug_not_found` for cross-tenant or absent slug — the
//     conflation prevents enumerating other tenants via status-code
//     differential).
//   - 422 → unexpected with the FastAPI validation envelope (invalid
//     slug, missing required field, both-or-neither directory and
//     tarball_url).
//   - 400 → unexpected with the backend's detail string
//     (`directory_not_found` / `not_a_directory` from the ingest
//     route).
//   - 501 → unexpected with the backend's detail (tarball_url ingest
//     is not implemented in v0.2).
//   - Any other 4xx/5xx → unexpected with the raw body.
//   - Pure transport errors → unreachable.
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
		// Ingest 400 = directory_not_found / not_a_directory from the
		// backplane substrate. Surface the detail verbatim so the
		// operator sees the path the backplane failed to read.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
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
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		// 422 from invalid slug shape, missing required body field,
		// or the exactly-one-of directory/tarball_url contract. The
		// backend emits the FastAPI validation envelope.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", he.Body)),
			jsonOut,
		)
	case http.StatusNotImplemented:
		// 501 from kb.ingest when the operator supplied
		// `tarball_url`. The substrate exposes only ingest_directory
		// in v0.2; the route surfaces a clear "not implemented" so
		// callers don't silently lose work.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
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
// Mirrors cli/internal/cmd/audit/audit.go::doAuthedRequest and its
// targets / operation siblings.
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

	// Read with a 1-MiB cap. The +1 byte over the cap is the
	// truncation-detection trick: if ReadAll returns more than
	// responseBodyCap bytes, the response was at least cap+1 bytes
	// long and the decoder would otherwise consume a silently-
	// truncated JSON payload. Fail loud instead — a truncated kb
	// response surfaces as "decode error: unexpected end of JSON
	// input" without this guard, which buries the real cause
	// (response too large for the chassis CLI's safety cap).
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
	// DELETE returns 204 with no body — the substrate of kb.delete
	// is idempotent and emits an empty body whether or not the row
	// existed. Treat 204 as success even when the response body is
	// empty so the verb's runner can render the success line without
	// trying to decode JSON.
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// responseBodyCap is the hard upper bound on a backplane response
// body the CLI is willing to read. Kb list pages cap at 500 rows
// server-side and each row carries only a 200-char preview; a full
// page is ~500 × 1 KB ≈ 500 KiB. 1 MiB is comfortable headroom.
// `meho kb show` of a real-world Markdown entry tops out at ~50 KB.
// The cap protects against an adversarial / misconfigured backplane
// sending an unbounded response — the alternative is OOM. The
// +1-byte read pattern in doAuthedRequest distinguishes "fits in
// the cap" from "truncated at the cap".
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
// pipe can't pin a kb add verb in unbounded ReadAll. 1 MiB matches
// the response-body cap and is generous for any realistic Markdown
// entry (the consumer's longest entries are ~10 KB).
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
