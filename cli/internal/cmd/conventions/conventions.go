// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package conventions hosts the cobra commands under `meho conventions ...`
// for G7.1-T3 (#315) of Initiative #229 (tenant conventions). v0.2 ships
// six operator-facing verbs that wrap the T2 REST surface
// (`backend/src/meho_backplane/api/v1/conventions.py`) shipped by
// G7.1-T2 (#314):
//
//   - `meho conventions list [--kind K] [--json]` — paginated listing via
//     GET /api/v1/conventions. Role: operator.
//   - `meho conventions show <slug> [--json]` — single-convention fetch
//     via GET /api/v1/conventions/{slug}. Role: operator. Renders the
//     body as plain Markdown unless --json is set.
//   - `meho conventions create --slug S --kind K --title T --body @file
//     [--priority N] [--json]` — create via POST /api/v1/conventions.
//     Role: tenant_admin.
//   - `meho conventions edit <slug> [--title T] [--body @file]
//     [--priority N] [--json]` — partial PATCH via PATCH
//     /api/v1/conventions/{slug}. Role: tenant_admin. With no --title /
//     --body / --priority flags, opens $EDITOR on the current body and
//     submits the saved content as a PATCH (the operator surface for
//     conversational rule edits).
//   - `meho conventions delete <slug> [--confirm] [--json]` — delete via
//     DELETE /api/v1/conventions/{slug}. Role: tenant_admin.
//   - `meho conventions history <slug> [--limit N] [--json]` — history
//     trail via GET /api/v1/conventions/{slug}/history. Role: operator.
//     Renders unified diffs between consecutive entries by default.
//
// Each verb wraps one backplane route. Authentication piggybacks on the
// token meho login wrote — same pattern as `meho kb`, `meho agent`,
// `meho audit`. The backend writes the audit_log row keyed by op_id
// (`conventions.<verb>`); the CLI just calls the route.
//
// The implementation deliberately follows the in-package HTTP helper
// pattern the sibling verb trees use (a local doAuthedRequest /
// renderRequestError pair) rather than a shared client package, for the
// import-cycle reason every sibling cites: each verb tree is grafted
// onto the root command, so a shared helper imported from cmd/* and
// from a per-tree package would close the cycle.
package conventions

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho conventions` parent command. Grafted onto
// the top-level meho tree by cmd/root.go alongside `meho kb`,
// `meho agent`, etc. The parent itself takes no args and prints its own
// help; every behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "conventions",
		Short: "Manage tenant conventions (list / show / create / edit / delete / history)",
		Long: "Manage tenant-scoped operational / workflow / reference " +
			"conventions wired by G7.1. A convention is a Markdown rule " +
			"the MEHO agent runtime auto-loads into the session preamble " +
			"(operational kind) or surfaces on demand (workflow / " +
			"reference). Write verbs (create / edit / delete) require " +
			"tenant_admin; read verbs (list / show / history) are " +
			"operator-level. Tenant scoping is enforced server-side via " +
			"the JWT — no surface accepts a tenant id, and cross-tenant " +
			"probes return 404 so existence is not leaked across tenant " +
			"boundaries.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newEditCmd())
	cmd.AddCommand(newDeleteCmd())
	cmd.AddCommand(newHistoryCmd())
	return cmd
}

// Convention mirrors the backend Convention pydantic model
// (`backend/src/meho_backplane/api/v1/conventions.py`). Hand-written
// rather than aliased to the generated oapi-codegen client type so the
// conventions package stays decoupled from generator churn — the kb /
// agent / audit packages take the same stance. `CreatedBySub` is a
// pointer because the seed migration (T5) inserts rows with no
// authenticated principal (the seed pre-dates any HTTP request).
type Convention struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	Slug         string  `json:"slug"`
	Title        string  `json:"title"`
	Body         string  `json:"body"`
	Kind         string  `json:"kind"`
	Priority     int     `json:"priority"`
	CreatedBySub *string `json:"created_by_sub"`
	CreatedAt    string  `json:"created_at"`
	UpdatedAt    string  `json:"updated_at"`
}

// Summary mirrors the backend ConventionSummary pydantic model — the
// lighter list-row shape returned by GET /api/v1/conventions. Omits the
// full `body` so a list of 20 conventions doesn't materialise 20 KB of
// rule text on every list call.
type Summary struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	Slug         string  `json:"slug"`
	Title        string  `json:"title"`
	Kind         string  `json:"kind"`
	Priority     int     `json:"priority"`
	CreatedBySub *string `json:"created_by_sub"`
	CreatedAt    string  `json:"created_at"`
	UpdatedAt    string  `json:"updated_at"`
}

// ListResponse mirrors the backend ConventionListResponse envelope —
// wrapped in `{"entries": [...]}` for forward-compat with future paging
// fields. Same shape kb / agent adopted.
type ListResponse struct {
	Entries []Summary `json:"entries"`
}

// HistoryEntry mirrors the backend ConventionHistoryEntry pydantic
// model. `BodyBefore` is a pointer because the first history row (the
// CREATE event) has no prior state; `AuditID` is a pointer because the
// T5 seed migration inserts history rows with no audit_log row.
type HistoryEntry struct {
	ID           string  `json:"id"`
	ConventionID string  `json:"convention_id"`
	BodyBefore   *string `json:"body_before"`
	BodyAfter    string  `json:"body_after"`
	ActorSub     string  `json:"actor_sub"`
	AuditID      *string `json:"audit_id"`
	Ts           string  `json:"ts"`
}

// validKinds mirrors the backend ConventionKind enum. CLI-side
// validation gives the operator an immediate rejection rather than a
// remote 422.
var validKinds = map[string]bool{"operational": true, "workflow": true, "reference": true}

// errMissingAccessToken is the sentinel doAuthedRequest returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure that renderRequestError maps to
// auth_expired with a `meho login` hint. Mirrors the kb / agent shape.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// renderRequestError translates a request error into the right
// output.StructuredError category. Maps the conventions REST surface's
// status codes:
//
//   - empty stored bearer → auth_expired.
//   - 401 (refresh failed) → auth_expired with a `meho login` hint.
//   - 403 → insufficient_role (the backend's detail names the required
//     role, typically "tenant_admin required" on write verbs).
//   - 404 → unexpected with the backend's detail (`convention_not_found`;
//     cross-tenant probes land here per the no-existence-leak posture).
//   - 409 → unexpected with the duplicate detail
//     (`convention_already_exists` on create with a slug that already
//     exists in the same tenant).
//   - 422 → unexpected with the validation envelope. The conventions
//     route has a domain-specific 422 the others don't: the over-budget
//     gate, raised when an `operational` body exceeds the preamble
//     token budget. The detail string is human-friendly verbatim
//     ("convention body exceeds preamble budget (estimated=X,
//     budget=Y)"); surface it as-is so the operator sees exactly how
//     many tokens over they are.
//   - Other 4xx/5xx → unexpected with the raw body.
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
	case http.StatusNotFound:
		// `show / edit / delete / history` on an absent slug surface
		// here; the backend's `convention_not_found` covers both genuine
		// absence and cross-tenant probes (the conflation prevents
		// enumerating other tenants via status-code differential). For
		// `list` / `create` (no slug in the path) a 404 means the route
		// doesn't exist on this backplane — typically an older deploy
		// without T2.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusConflict:
		// `create` with a slug that already exists in the same tenant
		// surfaces here (`convention_already_exists`).
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		// The conventions surface emits two distinct 422s:
		//   1. Pydantic validation envelope (`{"detail":[{...}]}`) on
		//      malformed input (invalid slug shape, missing field, bad
		//      kind value).
		//   2. The over-budget gate detail string when an `operational`
		//      body exceeds DEFAULT_MAX_PREAMBLE_TOKENS. Backend emits a
		//      string detail with the estimated and budget token counts
		//      so the operator can re-size the body precisely.
		//
		// decodeDetailString returns the string detail verbatim when the
		// JSON shape matches `{"detail": "..."}`; on the pydantic
		// envelope shape (`{"detail": [...]}`) it falls through to the
		// raw body. Both surface as unexpected so callers can branch
		// on exit code 4.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", decodeDetailString(he.Body))),
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
// JSON shape doesn't match (the pydantic validation envelope's `detail`
// is a list, not a string).
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
// api.IsNoRefreshToken / generic transport. A 204 yields a nil body
// without error (the DELETE verb hits this path).
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
	if resp.StatusCode == http.StatusNoContent {
		return nil, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &httpError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

// responseBodyCap bounds the response body the CLI will read. A
// convention body is a Markdown rule (the operational kind is hard-
// capped at the preamble token budget, ~3 KB; workflow / reference can
// be larger but ~50 KB is realistic). 1 MiB is comfortable headroom
// and protects against an adversarial / misconfigured backplane sending
// an unbounded response.
const responseBodyCap int64 = 1 << 20

// httpError carries a non-2xx response so per-verb runners render the
// right category.
type httpError struct {
	StatusCode int
	Body       string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Body)
}

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

// pathEscape escapes a single path segment.
func pathEscape(segment string) string {
	// url.PathEscape is the right call but we shouldn't import net/url
	// just for this; mirror the kb package's helper signature so the
	// per-verb buildShowPath / buildHistoryPath stays unit-testable.
	return urlPathEscape(segment)
}

// loadBodyFlag reads the --body flag value supporting inline text,
// `@<path>` for file references, and `@-` for stdin. Returns the
// loaded body or an error. The backend's `body` field has a min_length=1
// constraint, so an empty result is rejected at the CLI rather than
// after a 422 round-trip.
//
// `@-` reads from cmd.InOrStdin() so tests can wire a bytes.Buffer; the
// read is bounded by `loadBodyStdinCap` to protect against an
// adversarial pipe.
//
// Trailing newlines (LF or CRLF) are stripped from file / stdin reads
// so a 1-line file passed via `@` doesn't carry a gratuitous trailing
// newline through the JSON body. Embedded newlines inside the text are
// preserved; only the final CRLF / LF is stripped. Inline text values
// pass through verbatim.
//
// Mirrors `loadBodyFlag` in cli/internal/cmd/kb/kb.go so the surface
// shape (`--body @file` / `--body @-` / `--body "inline"`) is the same
// across the operator-facing verb trees.
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
			return "", errors.New("--body @- read empty input; cannot create convention with empty body")
		}
		return out, nil
	}
	blob, err := readBodyFile(path)
	if err != nil {
		return "", err
	}
	out := strings.TrimRight(string(blob), "\r\n")
	if out == "" {
		return "", fmt.Errorf("--body file %q is empty; cannot create convention with empty body", path)
	}
	return out, nil
}

// loadBodyStdinCap bounds the @- read so an adversarial / malformed
// pipe can't pin a create / edit verb in unbounded ReadAll. 256 KiB is
// generous for any realistic convention body (operational rules are
// bounded by the preamble token budget; reference material is the
// loose-bound case).
const loadBodyStdinCap int64 = 256 << 10

// readBodyFile is the file-read seam — split out so conventions_test.go
// can stub it deterministically without touching the filesystem.
var readBodyFile = func(path string) ([]byte, error) {
	blob, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read --body file %q: %w", path, err)
	}
	return blob, nil
}

// confirmPrompt prompts on stdin/stdout with the given message and
// returns true only when the operator types y/yes/Y. EOF (closed
// stdin) is treated as a no — scripted use must pass --confirm.
// Mirrors the kb / agent package's confirm helper.
func confirmPrompt(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.OutOrStdout(), "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		// Most common error: io.EOF from a piped empty stdin. Treat as
		// "no" so scripts that pipe /dev/null don't accidentally delete
		// a convention.
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// urlPathEscape escapes a path segment. Pulled out for a per-package
// seam — agents have a richer pathEscape with multi-segment handling;
// conventions doesn't need that level today.
func urlPathEscape(s string) string {
	// Per RFC 3986, a path segment may contain unreserved (ALPHA / DIGIT /
	// `-._~`), `pct-encoded`, sub-delims (`!$&'()*+,;=`), and `:@`.
	// Conventions slugs are constrained server-side to lowercase ASCII +
	// digits + hyphen (the V_SLUG pattern), so the escape is a no-op in
	// the realistic case — but we still escape defensively so an
	// adversarial slug (e.g., from a buggy script) can't smuggle a path
	// separator. Using net/url here directly to keep the implementation
	// stdlib-only and the code path obvious.
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case 'a' <= c && c <= 'z',
			'A' <= c && c <= 'Z',
			'0' <= c && c <= '9',
			c == '-', c == '.', c == '_', c == '~':
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// runEditor lives in editor.go alongside the other editor-related
// helpers — keep this file focused on the HTTP / auth helpers shared
// across the verb tree.
