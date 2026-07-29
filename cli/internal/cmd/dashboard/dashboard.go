// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package dashboard hosts the cobra commands under `meho dashboard ...` for
// Task #2590 of Initiative #2416 (parent goal #221, the deterministic check
// layer). Four operator-facing verbs wrap the Dashboard admin REST surface
// (`backend/src/meho_backplane/api/v1/checks_dashboards.py`), closing the
// composition gap that previously forced operators to hand-roll
// `POST /api/v1/checks/dashboards` with raw curl:
//
//   - `meho dashboard list [--tenant <id>] [--limit N] [--offset N] [--json]`
//     — list dashboards via GET /api/v1/checks/dashboards. Role: operator.
//     Each row carries the rolled-up state + member_count, so the list is
//     the "is everything OK?" glance surface.
//   - `meho dashboard show <dashboard_id> [--tenant <id>] [--json]` —
//     GET /api/v1/checks/dashboards/{id}. Role: operator. Renders the
//     five-state rollup plus every member's raw/effective state — the CLI
//     twin of /ui/checks/{dashboard_id}.
//   - `meho dashboard create --name <n> [--description <d>]
//     [--sensor-id <id> ...] [--tenant <id>] [--json]` — create one
//     dashboard via POST /api/v1/checks/dashboards. Role: tenant_admin. An
//     empty member set is legal and rolls up `unknown` (the zero-member
//     rule); a foreign / absent sensor id is refused 422 `sensor_not_found`.
//   - `meho dashboard delete <dashboard_id> [--tenant <id>] [--json]` —
//     DELETE /api/v1/checks/dashboards/{id}. Role: tenant_admin.
//
// Every verb drives the generated `api.ClientWithResponses` surface
// directly (the G0.12-T13 typed-client discipline): consumer-side struct
// drift can't recur because we consume `api.DashboardRead`,
// `api.DashboardListResponse`, `api.DashboardDetail`, and
// `api.DashboardCreate` directly. There is no update/edit verb by design —
// no PATCH route exists ("edit" is delete + recreate, the
// trigger-immutability posture).
package dashboard

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
	"unicode"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// NewRootCmd returns the `meho dashboard` parent command, grafted onto the
// top-level meho tree by cmd/root.go alongside `meho sensor`, etc.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "dashboard",
		Short: "Manage deterministic-check dashboards (list / show / create / delete)",
		Long: "Manage tenant-scoped dashboards wired by the deterministic " +
			"check layer (Initiative #2416). A dashboard composes registered " +
			"Sensors into a single rolled-up \"is everything OK?\" answer: the " +
			"worst-of the five-state member vocabulary (ok/degraded/critical/" +
			"skip/unknown), evaluated on read. Membership is set at create " +
			"only — there is no edit verb; \"edit\" is delete + recreate " +
			"(the trigger-immutability posture). Read verbs (list / show) are " +
			"operator-level; write verbs (create / delete) require " +
			"tenant_admin. Tenant scoping is enforced server-side via the " +
			"JWT; platform_admin callers may use --tenant to act on another " +
			"tenant's dashboards.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newShowCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newDeleteCmd())
	return cmd
}

// errMissingAccessToken is the sentinel newAuthedClient returns when the
// stored token row exists but its access_token field is empty. It's a
// credential-state failure, so renderRequestError maps it to auth_expired
// (exit 2) with a `meho login` hint.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// maxDashboardListRows mirrors the server-enforced list ceiling
// (dashboard_service.MAX_LIST_LIMIT, the `--limit` upper bound): a full page
// carries at most this many DashboardRead rows.
const maxDashboardListRows int64 = 500

// maxDashboardMembers mirrors the server-enforced membership cap
// (dashboard_schemas._MAX_MEMBERS): a detail read carries at most this many
// DashboardMemberView rows.
const maxDashboardMembers int64 = 200

// maxMemberRowBytes is a generous per-member ceiling for the body cap: the
// bounded columns (name<=128, connector_id/op_id<=256) plus the last_value /
// last_evidence projection, which is not tiny. 64 KiB/member keeps a valid
// full-membership detail well under the cap while still bounding an
// unbounded / adversarial body.
const maxMemberRowBytes int64 = 64 << 10

// responseBodyCap bounds the bytes the dashboard verb tree's transport reads
// off any backplane response body before surfacing *http.MaxBytesError.
// Sized from the largest legitimate response — a full-membership detail
// (200 members x 64 KiB = 12.8 MiB) dominates a full list page (500 rows x
// the small DashboardRead shape) — so no valid response trips the cap.
const responseBodyCap int64 = maxDashboardMembers * maxMemberRowBytes

// requestTimeout bounds a single dashboard request end-to-end (connect +
// headers + body read) so a hung backplane connection cannot pin a verb
// indefinitely -- the commands carry no context deadline of their own.
// Matches the 30 s convention the sibling sensor client uses.
const requestTimeout = 30 * time.Second

// capRoundTripper wraps an http.RoundTripper so every response body is
// re-bound to an http.MaxBytesReader before the typed-client parsers get a
// chance to drain it.
type capRoundTripper struct {
	base  http.RoundTripper
	limit int64
}

func (c *capRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := c.base.RoundTrip(req)
	if err != nil {
		return resp, err
	}
	if resp.Body != nil && c.limit > 0 {
		resp.Body = http.MaxBytesReader(nil, resp.Body, c.limit)
	}
	return resp, nil
}

// cappedHTTPClient returns an http.Client whose Transport caps every
// response body at responseBodyCap without mutating http.DefaultClient.
func cappedHTTPClient(base *http.Client) *http.Client {
	if base == nil {
		base = http.DefaultClient
	}
	clone := *base
	transport := clone.Transport
	if transport == nil {
		transport = http.DefaultTransport
	}
	clone.Transport = &capRoundTripper{base: transport, limit: responseBodyCap}
	if clone.Timeout == 0 {
		// Impose a finite request timeout only when the caller didn't set
		// one, so a future caller-supplied client keeps its own deadline.
		clone.Timeout = requestTimeout
	}
	return &clone
}

// newAuthedClient builds an api.AuthedClient for the supplied backplane URL
// with the response-body cap installed, and verifies a non-empty bearer is
// loaded.
func newAuthedClient(ctx context.Context, backplaneURL string) (*api.AuthedClient, error) {
	authed, err := api.NewAuthedClient(ctx, backplaneURL, api.AuthedClientOptions{
		HTTPClient: cappedHTTPClient(nil),
	})
	if err != nil {
		return nil, err
	}
	if authed.AccessToken() == "" {
		return nil, errMissingAccessToken
	}
	return authed, nil
}

// retryOn401 invokes call once, and if the typed response carries a 401,
// runs a one-shot bearer refresh and re-issues call.
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

// renderRequestError translates a transport-layer request error into the
// right output.StructuredError category. Parse / cap failures route to
// output.Unexpected (exit 4) rather than output.Unreachable (exit 3): a
// body-cap firing or a JSON decode rejecting a malformed payload is a
// server-side contract failure, not a transport-down failure.
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

// renderHTTPStatus classifies a non-2xx response carried in the typed
// envelope. Dashboard-route-specific notes:
//
//   - 403 — write to a dashboard the operator's role can't reach (an
//     operator-role caller using --tenant lands here too).
//   - 404 — `dashboard_not_found` covers both genuine absence and
//     cross-tenant probes (existence not leaked across tenants).
//   - 409 — `dashboard_name_conflict` on a create whose name is taken.
//   - 422 — FastAPI validation envelope (a foreign / absent member id
//     `sensor_not_found`, an over-cap member list); the verb surfaces the
//     backend detail cleanly rather than a stack trace.
//   - 401 — backplane rejected the stored token after a refresh attempt.
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
	case http.StatusConflict:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(decodeDetailString(bodyStr)),
			jsonOut,
		)
	case http.StatusUnprocessableEntity:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("invalid request: %s", decodeDetailString(bodyStr))),
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

// decodeDetailString pulls the `detail` field out of a FastAPI error body.
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

// sanitizeCell renders an untrusted persisted string safe for terminal
// output: control characters (including ESC, which drives ANSI/CSI escape
// sequences, and CR/LF, which could rewrite a table row) are replaced with
// U+FFFD so a crafted dashboard / sensor name cannot move the cursor,
// recolour, or clear the operator's terminal when a list or summary is
// printed. The --json path serialises the raw value unchanged (machine
// consumers re-escape).
func sanitizeCell(s string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return '�'
		}
		return r
	}, s)
}

// formatTime renders a *time.Time the way the backend ships it on the wire:
// ISO 8601 (RFC3339Nano) with sub-second precision when present.
func formatTime(t *time.Time) string {
	if t == nil {
		return ""
	}
	return t.Format(time.RFC3339Nano)
}

// derefString returns the pointed-to string, or "" for a nil pointer.
func derefString(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// printDashboardSummary renders one dashboard as a key-value summary,
// consuming api.DashboardDetail directly so drift in the backend model
// surfaces here at `go build` time rather than at runtime.
func printDashboardSummary(w io.Writer, d *api.DashboardDetail) {
	if d == nil {
		return
	}
	fmt.Fprintf(w, "%-18s %s\n", "id:", d.Id.String())
	fmt.Fprintf(w, "%-18s %s\n", "tenant_id:", d.TenantId.String())
	fmt.Fprintf(w, "%-18s %s\n", "name:", sanitizeCell(d.Name))
	if desc := derefString(d.Description); desc != "" {
		fmt.Fprintf(w, "%-18s %s\n", "description:", sanitizeCell(desc))
	}
	fmt.Fprintf(w, "%-18s %s\n", "state:", string(d.State))
	if d.LastRollupState != nil {
		fmt.Fprintf(w, "%-18s %s\n", "last_rollup_state:", string(*d.LastRollupState))
	}
	fmt.Fprintf(w, "%-18s %d\n", "member_count:", d.MemberCount)
	fmt.Fprintf(w, "%-18s %s\n", "created_by:", d.CreatedBySub)
	fmt.Fprintf(w, "%-18s %s\n", "created_at:", formatTime(&d.CreatedAt))
	fmt.Fprintf(w, "%-18s %s\n", "updated_at:", formatTime(&d.UpdatedAt))
}

// printMemberTable renders the per-member breakdown as a compact table:
// SENSOR_ID, NAME, RAW_STATE, EFFECTIVE_STATE, PENDING, SEVERITY, STATUS.
// It is the CLI twin of the /ui/checks/{id} member table.
func printMemberTable(w io.Writer, members []api.DashboardMemberView) {
	if len(members) == 0 {
		fmt.Fprintln(w, "no members (rolls up to unknown)")
		return
	}
	fmt.Fprintf(w, "\nmembers:\n")
	fmt.Fprintf(w, "%-36s %-20s %-10s %-15s %-8s %-9s %s\n",
		"SENSOR_ID", "NAME", "RAW_STATE", "EFFECTIVE_STATE", "PENDING", "SEVERITY", "STATUS")
	for _, m := range members {
		fmt.Fprintf(w, "%-36s %-20s %-10s %-15s %-8t %-9s %s\n",
			m.SensorId.String(), sanitizeCell(m.Name), string(m.RawState),
			string(m.EffectiveState), m.Pending, string(m.Severity), string(m.Status))
	}
}
