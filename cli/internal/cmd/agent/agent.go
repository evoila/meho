// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package agent hosts the cobra commands under `meho agent ...` for
// G11.1-T2 (#809) of Initiative #802 (the P1 agent runtime). v0.2 ships
// five operator-facing verbs that wrap the T2 REST surface
// (`backend/src/meho_backplane/api/v1/agents.py`):
//
//   - `meho agent list [--limit N] [--offset N] [--json]` — paginated
//     definition listing via GET /api/v1/agents. Role: operator.
//   - `meho agent show <name> [--json]` — single-definition fetch via
//     GET /api/v1/agents/{name}. Role: operator.
//   - `meho agent create <name> --identity-ref R --model-tier T
//     --system-prompt P --turn-budget N [--toolset @file] [--output-schema @file]
//     [--disabled] [--json]` — create via POST /api/v1/agents.
//     Role: tenant_admin.
//   - `meho agent edit <name> [--identity-ref R] [--model-tier T]
//     [--system-prompt P] [--turn-budget N] [--toolset @file]
//     [--output-schema @file] [--enabled|--disabled] [--json]` —
//     partial update via PATCH /api/v1/agents/{name}. Role: tenant_admin.
//   - `meho agent delete <name> [--confirm] [--json]` — delete via
//     DELETE /api/v1/agents/{name}. Role: tenant_admin.
//
// Authentication piggybacks on the token meho login wrote — same
// pattern as `meho kb`, `meho broadcast`, `meho audit`. RBAC at the
// backend rejects non-tenant_admin write callers with HTTP 403; the
// verbs render this as insufficient_role.
//
// The implementation follows the in-package HTTP helper pattern the
// sibling verb trees use (a local doAuthedRequest / renderRequestError
// pair) rather than a shared client package, for the import-cycle
// reason every sibling cites: each verb tree is grafted onto the root
// command, so a shared helper imported from cmd/* and from a per-tree
// package would close the cycle.
package agent

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

// NewRootCmd returns the `meho agent` parent command. Grafted onto the
// top-level meho tree by cmd/root.go alongside `meho kb`,
// `meho broadcast`, etc. The parent takes no args and prints its own
// help; every behaviour lives in the per-subcommand RunE closures.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "agent",
		Short: "Manage agent definitions (list / show / create / edit / delete)",
		Long: "Manage tenant-scoped agent definitions wired by G11.1. " +
			"An agent definition holds the identity reference, logical " +
			"model tier, system prompt, toolset spec, turn budget, and " +
			"optional output schema MEHO's agent runtime loads to run an " +
			"agent. Write verbs (create / edit / delete) require " +
			"tenant_admin; read verbs (list / show) are operator-level. " +
			"Tenant scoping is enforced server-side via the JWT — no " +
			"surface accepts a tenant id, and cross-tenant probes return " +
			"404 so existence is not leaked across tenant boundaries.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newEditCmd())
	cmd.AddCommand(newDeleteCmd())
	return cmd
}

// Entry mirrors the backend AgentDefinitionRead pydantic model
// (`backend/src/meho_backplane/api/v1/agents.py`). Hand-written rather
// than aliased to a generated client type so the agent package stays
// decoupled from oapi-codegen churn — the same stance the kb / audit /
// broadcast packages take. `OutputSchema` is a pointer so the JSON
// round-trip preserves the explicit-null wire shape (an agent with no
// structured-output schema has the field null).
type Entry struct {
	ID           string         `json:"id"`
	TenantID     string         `json:"tenant_id"`
	Name         string         `json:"name"`
	IdentityRef  string         `json:"identity_ref"`
	ModelTier    string         `json:"model_tier"`
	SystemPrompt string         `json:"system_prompt"`
	Toolset      map[string]any `json:"toolset"`
	TurnBudget   int            `json:"turn_budget"`
	OutputSchema map[string]any `json:"output_schema"`
	Enabled      bool           `json:"enabled"`
	CreatedBySub string         `json:"created_by_sub"`
	CreatedAt    string         `json:"created_at"`
	UpdatedAt    string         `json:"updated_at"`
}

// ListResponse mirrors the backend AgentDefinitionListResponse
// envelope (`{"agents": [...]}`) — wrapped for forward-compat with
// future paging fields.
type ListResponse struct {
	Agents []Entry `json:"agents"`
}

// validModelTiers mirrors the backend AgentModelTier enum. CLI-side
// validation gives the operator an immediate rejection rather than a
// remote 422.
var validModelTiers = map[string]bool{"standard": true, "fast": true, "deep": true}

// errMissingAccessToken is the sentinel doAuthedRequest returns when
// the stored token row exists but its access_token is empty — a
// credential-state failure that renderRequestError maps to
// auth_expired with a `meho login` hint. Mirrors the kb package's
// shape.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// renderRequestError translates a request error into the right
// output.StructuredError category. Maps the agents REST surface's
// status codes:
//
//   - empty stored bearer → auth_expired.
//   - 401 (refresh failed) → auth_expired with a `meho login` hint.
//   - 403 → insufficient_role.
//   - 404 → unexpected with the backend's detail (`agent_not_found`;
//     cross-tenant probes land here per the no-existence-leak posture).
//   - 409 → unexpected with the duplicate detail (`agent_already_exists`).
//   - 422 → unexpected with the FastAPI validation envelope.
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
		// `agent show / edit / delete` on an absent name surface here;
		// `agent_not_found` covers both genuine absence and cross-tenant
		// probes (the conflation prevents enumerating other tenants via
		// status-code differential). For `list` / `create` (no name in
		// the path) a 404 means the route doesn't exist on this
		// backplane — typically an older deploy without T2.
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(he.Body)),
			jsonOut,
		)
	case http.StatusConflict:
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

// responseBodyCap bounds the response body the CLI will read. An agent
// definition is a small structured record (system prompt + toolset
// spec); 1 MiB is comfortable headroom and protects against an
// adversarial / misconfigured backplane sending an unbounded response.
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

// printEntrySummary renders a definition as a key-value summary. The
// system prompt is reported by length (not dumped) so the summary stays
// scannable; operators wanting the full prompt use `--json`.
func printEntrySummary(w io.Writer, e *Entry) {
	if e == nil {
		return
	}
	fmt.Fprintf(w, "%-16s %s\n", "name:", e.Name)
	fmt.Fprintf(w, "%-16s %s\n", "id:", e.ID)
	fmt.Fprintf(w, "%-16s %s\n", "tenant_id:", e.TenantID)
	fmt.Fprintf(w, "%-16s %s\n", "identity_ref:", e.IdentityRef)
	fmt.Fprintf(w, "%-16s %s\n", "model_tier:", e.ModelTier)
	fmt.Fprintf(w, "%-16s %d\n", "turn_budget:", e.TurnBudget)
	fmt.Fprintf(w, "%-16s %t\n", "enabled:", e.Enabled)
	fmt.Fprintf(w, "%-16s %d bytes\n", "system_prompt:", len(e.SystemPrompt))
	fmt.Fprintf(w, "%-16s %t\n", "output_schema:", e.OutputSchema != nil)
	fmt.Fprintf(w, "%-16s %s\n", "created_by:", e.CreatedBySub)
	fmt.Fprintf(w, "%-16s %s\n", "created_at:", e.CreatedAt)
	fmt.Fprintf(w, "%-16s %s\n", "updated_at:", e.UpdatedAt)
}

// confirmPrompt prompts on stdin/stdout and returns true only when the
// operator types y/yes. EOF (closed stdin) is treated as a no — scripted
// use must pass --confirm. Mirrors the kb package's confirm helper.
func confirmPrompt(cmd *cobra.Command, prompt string) bool {
	fmt.Fprintf(cmd.OutOrStdout(), "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}

// jsonObjectCap bounds a --toolset / --output-schema @<path> or @- read
// so an adversarial / malformed file or pipe can't pin a create / edit
// verb in unbounded ReadAll. 256 KiB is generous for any realistic
// toolset spec or JSON Schema.
const jsonObjectCap int64 = 256 << 10

// readJSONFile is the file-read seam — a var so the unit tests can stub
// it deterministically without touching the filesystem.
var readJSONFile = func(path string) ([]byte, error) {
	return os.ReadFile(path)
}

// loadJSONObjectFlag reads a flag value that carries a JSON object,
// supporting inline JSON, `@<path>` for a file, and `@-` for stdin. It
// returns nil for an empty value so the caller can omit the field from
// the request body entirely. The decoded value must be a JSON object
// (the backend's toolset / output_schema fields are objects); a non-
// object (array, scalar) is rejected at the CLI rather than after a
// remote 422.
func loadJSONObjectFlag(cmd *cobra.Command, raw, flagName string) (map[string]any, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	var blob []byte
	switch {
	case raw == "@-":
		read, err := io.ReadAll(io.LimitReader(cmd.InOrStdin(), jsonObjectCap+1))
		if err != nil {
			return nil, fmt.Errorf("read %s from stdin: %w", flagName, err)
		}
		if int64(len(read)) > jsonObjectCap {
			return nil, fmt.Errorf("%s from stdin exceeds %d-byte cap", flagName, jsonObjectCap)
		}
		blob = read
	case strings.HasPrefix(raw, "@"):
		path := strings.TrimPrefix(raw, "@")
		read, err := readJSONFile(path)
		if err != nil {
			return nil, fmt.Errorf("read %s file %q: %w", flagName, path, err)
		}
		blob = read
	default:
		blob = []byte(raw)
	}
	var out map[string]any
	if err := json.Unmarshal(blob, &out); err != nil {
		return nil, fmt.Errorf("%s must be a JSON object: %w", flagName, err)
	}
	return out, nil
}
