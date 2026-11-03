// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

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

// validSafetyLevels pins the safety_level enum the backend accepts.
// Matches the safety_level column on endpoint_descriptor (G0.6-T1
// #392 schema). Fail fast in the CLI rather than letting the
// backplane 422 surface an unfamiliar value.
//
// Mirrors the generated `api.EditOpBodySafetyLevel` enum constants
// (`EditOpBodySafetyLevelSafe` / `…Caution` / `…Dangerous` /
// `…Destructive`) so the
// validator and the wire shape stay in lockstep. The generator-shipped
// enum values are the authoritative source — we keep this map only to
// render the verb's helptext + the "expected one of" message in the same
// order the operator sees in --help.
var validSafetyLevels = map[string]api.EditOpBodySafetyLevel{
	"safe":        api.EditOpBodySafetyLevelSafe,
	"caution":     api.EditOpBodySafetyLevelCaution,
	"dangerous":   api.EditOpBodySafetyLevelDangerous,
	"destructive": api.EditOpBodySafetyLevelDestructive,
}

// newEditOpCmd returns the `meho connector edit-op` command.
//
// CLI shape:
//
//	meho connector edit-op <connector_id> <op_id> \
//	  [--custom-description <text>|@<file>] \
//	  [--safety safe|caution|dangerous|destructive] \
//	  [--requires-approval | --no-requires-approval] \
//	  [--enable | --disable] \
//	  [--reason <text>] \
//	  [--json] [--backplane <url>]
//
// Hits PATCH /api/v1/connectors/<connector_id>/operations/<op_id>
// with an api.EditOpBody. tenant_admin role required.
func newEditOpCmd() *cobra.Command {
	var (
		customDesc        string
		safetyFlag        string
		requiresApproval  bool
		clearApproval     bool
		enableOp          bool
		disableOp         bool
		reason            string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "edit-op <connector_id> <op_id>",
		Short: "Patch a per-op override (custom_description, safety, approval, enabled)",
		Long: "edit-op calls PATCH /api/v1/connectors/<id>/operations/<op_id>\n" +
			"to override the per-op flags an operator wants to differ from\n" +
			"the parser-derived defaults. Common patterns:\n\n" +
			"  --safety dangerous            # parser tagged a destructive\n" +
			"                                # POST as 'caution'; bump it.\n" +
			"  --requires-approval           # gate this op on the approval\n" +
			"                                # workflow regardless of safety.\n" +
			"  --no-requires-approval --reason <text>\n" +
			"                                # turn approval off (platform admin).\n" +
			"  --custom-description @path.md # operator-authored agent prompt\n" +
			"                                # supersedes vendor docs.\n" +
			"  --disable                     # exclude this op from\n" +
			"                                # search_operations + dispatch\n" +
			"                                # even when the connector is enabled.\n" +
			"  --enable                      # re-include a previously disabled op.\n\n" +
			"op_id is the canonical form `METHOD:/path` for generic\n" +
			"connectors (e.g. `GET:/api/vcenter/cluster`). The CLI URL-escapes\n" +
			"the op_id segment before placing it in the path so the colons\n" +
			"and slashes survive the routing layer.\n\n" +
			"--enable on an op whose resolved connector is still the\n" +
			"unconfigured spec-ingest auto-shim succeeds but prints a\n" +
			"`warning (unreplaced_auto_shim)` to stderr: dispatch will fail\n" +
			"until the per-product Connector subclass is registered, and\n" +
			"re-ingesting the spec will not replace the shim.\n\n" +
			"Risk level and approval are stored apart from the value in code,\n" +
			"so they stay after a restart and a re-ingest. --safety can only\n" +
			"raise the level; to lower the classification, open a pull request.\n" +
			"A tenant admin's change applies to their own tenant; a platform\n" +
			"admin's change to a built-in connector applies to the whole install.\n\n" +
			"Turning approval off (--no-requires-approval on an operation whose\n" +
			"code requires approval) needs platform_admin, a --reason, and a\n" +
			"fresh sign-in: run `meho login` and retry within five minutes. It is\n" +
			"never allowed for a destructive operation. List these operations\n" +
			"with `meho connector overrides --reduced-approval`.\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEditOp(cmd, editOpOptions{
				ConnectorID:          args[0],
				OpID:                 args[1],
				CustomDescriptionRaw: customDesc,
				SafetyFlag:           safetyFlag,
				RequiresApproval:     requiresApproval,
				ClearApproval:        clearApproval,
				EnableOp:             enableOp,
				DisableOp:            disableOp,
				Reason:               reason,
				JSONOut:              jsonOut,
				BackplaneOverride:    backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&customDesc, "custom-description", "",
		"replacement custom_description; supports `@<path>` to read from a file")
	cmd.Flags().StringVar(&safetyFlag, "safety", "",
		"replacement safety_level: safe | caution | dangerous | destructive (can only raise the level)")
	cmd.Flags().BoolVar(&requiresApproval, "requires-approval", false,
		"mark the op as requiring an approval workflow")
	cmd.Flags().BoolVar(&clearApproval, "no-requires-approval", false,
		"clear the requires_approval flag")
	cmd.Flags().BoolVar(&enableOp, "enable", false,
		"set is_enabled=true on this op")
	cmd.Flags().BoolVar(&disableOp, "disable", false,
		"set is_enabled=false on this op")
	cmd.Flags().StringVar(&reason, "reason", "",
		"why you make this change; written to the audit row and required to turn approval off")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.MarkFlagsMutuallyExclusive("requires-approval", "no-requires-approval")
	cmd.MarkFlagsMutuallyExclusive("enable", "disable")
	return cmd
}

type editOpOptions struct {
	ConnectorID          string
	OpID                 string
	CustomDescriptionRaw string
	SafetyFlag           string
	RequiresApproval     bool
	ClearApproval        bool
	EnableOp             bool
	DisableOp            bool
	Reason               string
	JSONOut              bool
	BackplaneOverride    string
}

func runEditOp(cmd *cobra.Command, opts editOpOptions) error {
	body := api.EditOpBody{}

	descText, descSet, err := loadTextFlag(cmd, "custom-description")
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), opts.JSONOut)
	}
	if descSet {
		body.CustomDescription = &descText
	}

	if opts.SafetyFlag != "" {
		level, ok := validSafetyLevels[opts.SafetyFlag]
		if !ok {
			return output.RenderError(cmd.ErrOrStderr(),
				output.Unexpected(fmt.Sprintf(
					"--safety %q invalid; expected one of: safe | caution | dangerous | destructive",
					opts.SafetyFlag,
				)),
				opts.JSONOut,
			)
		}
		body.SafetyLevel = &level
	}

	if opts.RequiresApproval {
		t := true
		body.RequiresApproval = &t
	} else if opts.ClearApproval {
		f := false
		body.RequiresApproval = &f
	}

	if opts.EnableOp {
		t := true
		body.IsEnabled = &t
	} else if opts.DisableOp {
		f := false
		body.IsEnabled = &f
	}

	if opts.Reason != "" {
		reason := opts.Reason
		body.Reason = &reason
	}

	if isEmptyEditOpBody(body) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected("edit-op needs at least one of --custom-description / --safety / --requires-approval / --no-requires-approval / --enable / --disable"),
			opts.JSONOut,
		)
	}

	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	patched, err := patchOp(cmd.Context(), backplaneURL, opts.ConnectorID, opts.OpID, body)
	if err != nil {
		var he *httpResponseError
		if errors.As(err, &he) {
			if he.statusCode == http.StatusUnauthorized && isFreshSignInRequired(he.body) {
				return output.RenderError(cmd.ErrOrStderr(),
					output.AuthExpired(fmt.Sprintf(
						"%s Run `meho login %s` and retry.",
						strings.TrimSpace(httpErrorDetail(he.body)), backplaneURL,
					)),
					opts.JSONOut,
				)
			}
			return renderHTTPStatus(cmd, backplaneURL, he.statusCode, he.body, opts.JSONOut)
		}
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	// Enable-time advisories land on stderr in BOTH output modes
	// (G0.23-T4 #1630): stdout stays the machine surface (--json) /
	// the success summary, while "this enable is a dispatch dead end"
	// must reach the operator even when stdout is piped into jq. The
	// same warnings ride the --json envelope as structured fields.
	printEditOpWarnings(cmd.ErrOrStderr(), patched.Warnings)
	if opts.JSONOut {
		// Echo the operator's PATCH body plus the backplane's
		// advisories — the route's 200 EditOpResponse carries only
		// `warnings`, so the echoed payload remains the structured
		// record of what was edited. Same shape as edit-group's
		// --json output, extended with `warnings`.
		return output.PrintJSON(cmd.OutOrStdout(), editOpResult{
			ConnectorID: opts.ConnectorID,
			OpID:        opts.OpID,
			Patched:     body,
			Warnings:    patched.Warnings,
		})
	}
	printEditOpResult(cmd.OutOrStdout(), opts.ConnectorID, opts.OpID, body)
	return nil
}

// isEmptyEditOpBody returns true when no field is set — the operator
// invoked edit-op without any actual change. Without this guard the
// CLI would ship an empty PATCH that succeeds at the route layer
// but does nothing, surprising the operator.
func isEmptyEditOpBody(b api.EditOpBody) bool {
	return b.CustomDescription == nil &&
		b.SafetyLevel == nil &&
		b.RequiresApproval == nil &&
		b.IsEnabled == nil
}

// patchOp drives the typed-client edit-op endpoint with a one-shot
// 401-retry. The route returns HTTP 200 with an EditOpResponse
// envelope (G0.23-T4 #1630 promoted it from 204 No Content so
// enable-time advisories have a structured wire home); the decoded
// JSON200 payload is returned for the caller to render. Non-200
// surfaces as *httpResponseError for the caller to route through
// renderHTTPStatus. The op_id segment may contain `:` and `/`
// (canonical form `METHOD:/path`); the generated client URL-escapes
// the path parameter, so the colons / slashes survive the routing
// layer.
func patchOp(
	ctx context.Context,
	backplaneURL, connectorID, opID string,
	body api.EditOpBody,
) (*api.EditOpResponse, error) {
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return nil, err
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchResponse, error) {
			return authed.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchWithResponse(
				ctx,
				connectorID,
				opID,
				&api.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchParams{},
				body,
			)
		},
		func(r *api.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
		// A 200 whose body failed to decode (JSON200 nil) is the
		// same contract-drift class as a non-200: surface the raw
		// body so renderHTTPStatus can classify it.
		return nil, &httpResponseError{statusCode: resp.StatusCode(), body: resp.Body}
	}
	return resp.JSON200, nil
}

// editOpResult is the synthetic envelope rendered to JSON for --json
// operators. The PATCH route's 200 response carries only `warnings`,
// so the echoed PATCH body remains the structured record of the edit;
// `warnings` relays the backplane's enable-time advisories verbatim.
type editOpResult struct {
	ConnectorID string              `json:"connector_id"`
	OpID        string              `json:"op_id"`
	Patched     api.EditOpBody      `json:"patched"`
	Warnings    []api.EditOpWarning `json:"warnings"`
}

// printEditOpWarnings renders the backplane's enable-time advisories
// to stderr, one line per warning, prefixed with the stable code so
// operators (and log scrapers) can grep for `unreplaced_auto_shim`.
// No-op on the clean path.
func printEditOpWarnings(w io.Writer, warnings []api.EditOpWarning) {
	for _, warning := range warnings {
		fmt.Fprintf(w, "warning (%s): %s\n", warning.Code, warning.Message)
	}
}

// freshSignInCode is the classifier the backplane puts at the start of
// the 401 detail when an action needs a fresh sign-in (#4711).
const freshSignInCode = "approval_off_needs_fresh_sign_in"

// isFreshSignInRequired reports whether a 401 body is the fresh sign-in
// refusal rather than an expired or invalid token.
func isFreshSignInRequired(body []byte) bool {
	return strings.HasPrefix(httpErrorDetail(body), freshSignInCode)
}

// httpErrorDetail returns the FastAPI `detail` string of an error body,
// or the raw body when it has no string `detail`.
func httpErrorDetail(body []byte) string {
	var parsed struct {
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal(body, &parsed); err == nil && parsed.Detail != "" {
		return parsed.Detail
	}
	return strings.TrimSpace(string(body))
}

func printEditOpResult(w io.Writer, connectorID, opID string, body api.EditOpBody) {
	fmt.Fprintf(w, "%s/%s — updated\n", connectorID, opID)
	if body.SafetyLevel != nil {
		fmt.Fprintf(w, "  safety: %s\n", string(*body.SafetyLevel))
	}
	if body.RequiresApproval != nil {
		fmt.Fprintf(w, "  requires_approval: %t\n", *body.RequiresApproval)
	}
	if body.IsEnabled != nil {
		fmt.Fprintf(w, "  is_enabled: %t\n", *body.IsEnabled)
	}
	if body.Reason != nil {
		fmt.Fprintf(w, "  reason: %s\n", *body.Reason)
	}
	if body.CustomDescription != nil && *body.CustomDescription != "" {
		fmt.Fprintf(w, "  custom_description: %s\n", *body.CustomDescription)
	}
}
