// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package sensor hosts the cobra commands under `meho sensor ...` for
// Task #2503 of Initiative #2416 (parent goal #221, the deterministic
// check layer). Three operator-facing verbs wrap the Sensor admin REST
// surface (`backend/src/meho_backplane/api/v1/sensors.py`):
//
//   - `meho sensor list [--status <s>] [--cadence-kind <k>] [--tenant <id>]
//     [--limit N] [--offset N] [--json]` — list sensors via
//     GET /api/v1/sensors. Role: operator. The list carries each sensor's
//     latest-result projection, so it is also the status view.
//   - `meho sensor create --name <n> --connector-id <id> --op-id <op>
//     --assertion <json> --cadence-kind <interval|cron>
//     (--interval-seconds N | --cron-expr <expr>) [--timezone <tz>]
//     [--severity <degraded|critical>] [--for-seconds N] [--target <json>]
//     [--params <json>] [--identity-sub <sub>] [--tenant <id>] [--json]` —
//     create one sensor via POST /api/v1/sensors. Role: tenant_admin. The
//     op must resolve to a safety_level='safe' descriptor.
//   - `meho sensor delete <sensor_id> [--tenant <id>] [--json]` —
//     hard-delete one sensor via DELETE /api/v1/sensors/{id}.
//     Role: tenant_admin.
//
// Every verb drives the generated `api.ClientWithResponses` surface
// directly (the G0.12-T13 typed-client discipline): consumer-side struct
// drift can't recur because we consume `api.SensorRead`,
// `api.SensorListResponse`, and `api.SensorCreate` directly.
package sensor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// jsonObjectCap bounds an --assertion / --target / --params @<path> or @-
// read so an adversarial / malformed file or pipe can't pin a create verb
// in unbounded ReadAll. 256 KiB is generous for any realistic assertion or
// params payload.
const jsonObjectCap int64 = 256 << 10

// readJSONFile is the file-read seam — a var so unit tests can stub it
// deterministically. The implementation enforces jsonObjectCap so a
// multi-GiB JSON file passed via `@<path>` cannot OOM the CLI.
var readJSONFile = func(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	read, err := io.ReadAll(io.LimitReader(f, jsonObjectCap+1))
	if err != nil {
		return nil, err
	}
	if int64(len(read)) > jsonObjectCap {
		return nil, fmt.Errorf("file %q exceeds %d-byte cap", path, jsonObjectCap)
	}
	return read, nil
}

// loadJSONObjectBytes reads a flag value that carries a JSON object,
// supporting inline JSON, `@<path>` for a file, and `@-` for stdin. It
// returns nil for an empty value so the caller can omit the field. The
// decoded value must be a JSON object; a non-object (array, scalar, or
// JSON `null`) is rejected at the CLI rather than after a remote 422.
// JSON `null` is rejected explicitly because json.Unmarshal of `null` into
// map[string]any sets the map to nil without an error — a silent accept
// would forward an empty field the backend cannot disambiguate from
// "omitted". Returns the raw object bytes so a caller that needs the
// generated union type (the assertion spec) can unmarshal them directly.
func loadJSONObjectBytes(cmd *cobra.Command, raw, flagName string) ([]byte, error) {
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
	var probe map[string]any
	if err := json.Unmarshal(blob, &probe); err != nil {
		return nil, fmt.Errorf("%s must be a JSON object: %w", flagName, err)
	}
	if probe == nil {
		return nil, fmt.Errorf("%s must be a JSON object (got null)", flagName)
	}
	return blob, nil
}

// loadJSONObjectFlag is the map-returning variant used for --target /
// --params, where the backend field is a plain object map.
func loadJSONObjectFlag(cmd *cobra.Command, raw, flagName string) (map[string]any, error) {
	blob, err := loadJSONObjectBytes(cmd, raw, flagName)
	if err != nil || blob == nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(blob, &out); err != nil {
		return nil, fmt.Errorf("%s must be a JSON object: %w", flagName, err)
	}
	return out, nil
}

// NewRootCmd returns the `meho sensor` parent command, grafted onto the
// top-level meho tree by cmd/root.go alongside `meho scheduler`, etc.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "sensor",
		Short: "Manage deterministic-check sensors (list / create / delete)",
		Long: "Manage tenant-scoped sensors wired by the deterministic " +
			"check layer (Initiative #2416). A sensor pins an (op + args + " +
			"assertion + cadence + severity) tuple the check runner " +
			"evaluates on a schedule (kind=interval for sub-minute cadence, " +
			"kind=cron for >=1-minute cadence). The op must be safe (a " +
			"caution/dangerous op is refused at create). Write verbs " +
			"(create / delete) require tenant_admin; the read verb (list) is " +
			"operator-level. Tenant scoping is enforced server-side via the " +
			"JWT; platform_admin callers may use --tenant to act on another " +
			"tenant's sensors.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newListCmd())
	cmd.AddCommand(newCreateCmd())
	cmd.AddCommand(newDeleteCmd())
	return cmd
}

// validCadenceKinds mirrors the backend SensorCadenceKind enum.
var validCadenceKinds = map[string]bool{"interval": true, "cron": true}

// validStatuses mirrors the backend SensorStatus enum.
var validStatuses = map[string]bool{"active": true, "paused": true}

// validSeverities mirrors the backend SensorSeverity enum.
var validSeverities = map[string]bool{"degraded": true, "critical": true}

// errMissingAccessToken is the sentinel newAuthedClient returns when the
// stored token row exists but its access_token field is empty. It's a
// credential-state failure, so renderRequestError maps it to auth_expired
// (exit 2) with a `meho login` hint.
var errMissingAccessToken = errors.New("meho: stored token has no access_token")

// responseBodyCap bounds the bytes the sensor verb tree's transport reads
// off any backplane response body before surfacing *http.MaxBytesError.
// 1 MiB is generous for a paginated sensor list (limit 500 with full row
// payloads including evidence maps stays well under it).
const responseBodyCap int64 = 1 << 20

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
// envelope. Sensor-route-specific notes:
//
//   - 403 — write to a sensor the operator's role can't reach (the
//     operator-role caller using --tenant lands here too).
//   - 404 — `sensor_not_found` covers both genuine absence and
//     cross-tenant probes (existence not leaked across tenants).
//   - 409 — `sensor_name_conflict` on a create whose name is taken.
//   - 422 — FastAPI validation envelope (safe-only op guard
//     `sensor_requires_safe_operation` / `sensor_operation_not_found`,
//     malformed assertion / cadence); the verb surfaces the backend detail.
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

// printSensorSummary renders one sensor as a key-value summary, consuming
// api.SensorRead directly so drift in the backend model surfaces here at
// `go build` time rather than at runtime.
func printSensorSummary(w io.Writer, s *api.SensorRead) {
	if s == nil {
		return
	}
	fmt.Fprintf(w, "%-22s %s\n", "id:", s.Id.String())
	fmt.Fprintf(w, "%-22s %s\n", "tenant_id:", s.TenantId.String())
	fmt.Fprintf(w, "%-22s %s\n", "name:", s.Name)
	fmt.Fprintf(w, "%-22s %s\n", "connector_id:", s.ConnectorId)
	fmt.Fprintf(w, "%-22s %s\n", "op_id:", s.OpId)
	fmt.Fprintf(w, "%-22s %s\n", "status:", string(s.Status))
	if s.StatusReason != nil && *s.StatusReason != "" {
		fmt.Fprintf(w, "%-22s %s\n", "status_reason:", *s.StatusReason)
	}
	fmt.Fprintf(w, "%-22s %s\n", "cadence_kind:", string(s.CadenceKind))
	if s.IntervalSeconds != nil {
		fmt.Fprintf(w, "%-22s %ds\n", "interval_seconds:", *s.IntervalSeconds)
	}
	if s.CronExpr != nil {
		fmt.Fprintf(w, "%-22s %s\n", "cron_expr:", *s.CronExpr)
		fmt.Fprintf(w, "%-22s %s\n", "timezone:", s.Timezone)
	}
	if s.NextFireAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "next_fire_at:", formatTime(s.NextFireAt))
	}
	fmt.Fprintf(w, "%-22s %s\n", "severity:", string(s.Severity))
	fmt.Fprintf(w, "%-22s %d\n", "for_seconds:", s.ForSeconds)
	fmt.Fprintf(w, "%-22s %s\n", "last_state:", string(s.LastState))
	if s.LastEvaluatedAt != nil {
		fmt.Fprintf(w, "%-22s %s\n", "last_evaluated_at:", formatTime(s.LastEvaluatedAt))
	}
	if s.StateSince != nil {
		fmt.Fprintf(w, "%-22s %s\n", "state_since:", formatTime(s.StateSince))
	}
	fmt.Fprintf(w, "%-22s %s\n", "identity_sub:", s.IdentitySub)
	fmt.Fprintf(w, "%-22s %s\n", "created_by:", s.CreatedBySub)
	fmt.Fprintf(w, "%-22s %s\n", "created_at:", formatTime(&s.CreatedAt))
}

// formatTime renders a *time.Time the way the backend ships it on the wire:
// ISO 8601 (RFC3339Nano) with sub-second precision when present.
func formatTime(t *time.Time) string {
	if t == nil {
		return ""
	}
	return t.Format(time.RFC3339Nano)
}
