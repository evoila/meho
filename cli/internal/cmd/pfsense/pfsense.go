// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package pfsense hosts the cobra commands under `meho pfsense ...` for
// G3.7-T3 (#850) of Initiative #370. The verb tree is a thin Cobra
// layer over `POST /api/v1/operations/call`, identical pattern to
// `meho bind9 ...` (#591) and `meho k8s ...` (#326), pre-baking the
// `connector_id="pfsense-ssh-2.7"` argument so operators don't type
// the connector ID on every dispatch:
//
//   - `meho pfsense about [--target T]`          — pfsense.about
//   - `meho pfsense version [--target T]`        — pfsense.version
//   - `meho pfsense firewall rules [--target T]` — pfsense.firewall.rules
//   - `meho pfsense firewall state [--target T]` — pfsense.firewall.state
//   - `meho pfsense nat rules [--target T]`      — pfsense.nat.rules
//   - `meho pfsense network interface [--target T]` — pfsense.interface.list
//   - `meho pfsense network gateway [--target T]`   — pfsense.gateway.list
//   - `meho pfsense config show [--target T]`    — pfsense.config.show
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — the CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// The verb tree replaces the consumer's `scripts/pfsense.sh` wrapper
// (retiring the script surface documented in #850). The
// `meho pfsense` surface is operator-only — the MCP / agent surface
// continues to reach pfSense ops via the narrow-waist search_operations
// + call_operation meta-tools (CLAUDE.md postulate 5).
package pfsense

import (
	"errors"
	"fmt"
	"io"
	"net/url"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho pfsense ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future re-versioning
// (pfsense-2.8 or pfsense-2.x) lands as a single line edit here.
//
// The id encodes the registry-v2 natural key triple
// "(product="pfsense", version="2.7", impl_id="pfsense-ssh")" per the
// connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`:
// the trailing `-2.7` segment is the version; everything before is the
// impl_id (`pfsense-ssh`); the product is the first hyphen segment of
// the impl_id (`pfsense`). The impl_id discriminator (`-ssh`) leaves
// room for a future non-SSH control surface without breaking the
// resolver's tie-break ladder.
const ConnectorID = "pfsense-ssh-2.7"

// NewRootCmd returns the `meho pfsense` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verb trees. The parent itself takes no args and prints its
// own help; every piece of behaviour lives in the per-subcommand RunE
// closures.
//
// Sub-tree layout follows the issue #850 verb table:
//   - `pfsense about`             — flat verb
//   - `pfsense version`           — flat verb
//   - `pfsense firewall <rules|state>` — sub-tree
//   - `pfsense nat rules`         — sub-tree
//   - `pfsense network <interface|gateway>` — sub-tree
//   - `pfsense config show`       — sub-tree
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "pfsense",
		Short: "Pre-scoped CLI verbs for the pfsense-ssh-2.7 connector",
		Long: "pfsense is the operator-facing verb tree for the pfsense-ssh-2.7\n" +
			"connector (registry triple (product=\"pfsense\", version=\"2.7\",\n" +
			"impl_id=\"pfsense-ssh\")). Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\"pfsense-ssh-2.7\"\n" +
			"pre-baked so operators don't type the connector ID on every\n" +
			"command. All shipped ops are read-only; write ops are out of\n" +
			"scope for G3.7 (#370).\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.\n\n" +
			"Target note: pfSense's admin user lands in an interactive console\n" +
			"menu by default. The connector asserts shell access (probe returns\n" +
			"no_shell_access if the session drops into the console menu) — the\n" +
			"SSH user must have shell access configured (see\n" +
			"docs/cross-repo/pfsense-onboarding.md).",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newVersionCmd())
	cmd.AddCommand(newFirewallCmd())
	cmd.AddCommand(newNatCmd())
	cmd.AddCommand(newNetworkCmd())
	cmd.AddCommand(newConfigCmd())
	return cmd
}

// errNoBackplaneConfigured wraps auth.ErrConfigNotFound so callers
// can distinguish "operator never logged in" (→ auth_expired exit
// code 2 — the right fix is `meho login`) from URL-parse failures
// (→ unexpected exit code 4 — the right fix is correcting argv).
type errNoBackplaneConfigured struct{ inner error }

func (e *errNoBackplaneConfigured) Error() string {
	return "no backplane URL configured; run `meho login <url>` first or pass --backplane <url>"
}
func (e *errNoBackplaneConfigured) Unwrap() error { return e.inner }

// resolveBackplane mirrors the bind9 / k8s sibling helpers:
// --backplane override flag wins; otherwise read the URL the most
// recent `meho login` wrote to config.json.
func resolveBackplane(override string) (string, error) {
	if override != "" {
		return normaliseURL(override)
	}
	cfg, err := auth.LoadConfig()
	if err != nil {
		if errors.Is(err, auth.ErrConfigNotFound) {
			return "", &errNoBackplaneConfigured{inner: err}
		}
		return "", err
	}
	return normaliseURL(cfg.BackplaneURL)
}

// classifyBackplaneError maps a resolveBackplane error to the right
// output.StructuredError category. Identical routing to the bind9 / k8s
// siblings: missing-config → auth_expired; everything else → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the bind9 / k8s siblings.
func normaliseURL(s string) (string, error) {
	trimmed := strings.TrimRight(strings.TrimSpace(s), "/")
	if trimmed == "" {
		return "", errors.New("backplane URL is empty")
	}
	u, err := url.ParseRequestURI(trimmed)
	if err != nil {
		return "", fmt.Errorf("invalid backplane URL %q: %w", s, err)
	}
	if u.Host == "" {
		return "", fmt.Errorf("backplane URL %q has no host", s)
	}
	u.Path = strings.TrimRight(u.Path, "/")
	return u.String(), nil
}

// renderRequestError translates an error from doAuthedRequest into
// the right output.RenderError category. Same classification ladder
// as the bind9 sibling: token-not-found / no-refresh-token →
// auth_expired; HTTP-error → unexpected; transport → unreachable.
func renderRequestError(
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
	if api.IsNoRefreshToken(err) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.AuthExpired(fmt.Sprintf(
				"stored token rejected and no refresh_token present; run `meho login %s`",
				backplaneURL,
			)),
			jsonOut,
		)
	}
	var apiErr *dispatch.APIResponseError
	if errors.As(err, &apiErr) {
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf("call %s: HTTP %d: %s",
				backplaneURL, apiErr.StatusCode, apiErr.Body)),
			jsonOut,
		)
	}
	return output.RenderError(cmd.ErrOrStderr(),
		output.Unreachable(fmt.Sprintf("call %s: %v", backplaneURL, err)),
		jsonOut,
	)
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in interface names survives without producing an invalid UTF-8
// cut. Same implementation as the bind9 / k8s siblings.
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

// fallbackResultRender dumps the result envelope verbatim when the
// typed per-verb decode fails.
func fallbackResultRender(w io.Writer, r *CallResult) {
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	pretty, err := dispatch.PrettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}

// stringField pulls a string field from a row entry, returning empty
// string when the field is missing or wrong type.
func stringField(e map[string]any, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}
