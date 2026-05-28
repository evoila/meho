// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package holodeck hosts the cobra commands under `meho holodeck ...`
// for G3.8-T3 (#855) of Initiative #371 (G3.8 Holodeck typed-SSH
// connector). The verb tree is a thin Cobra layer over
// `POST /api/v1/operations/call`, identical pattern to
// `meho pfsense ...` (#850), `meho bind9 ...` (#591), and
// `meho k8s ...` (#326), pre-baking the
// `connector_id="holodeck-ssh-9.0"` argument so operators don't type
// the connector ID on every dispatch:
//
//   - `meho holodeck about [--target T]`                       — holodeck.about
//   - `meho holodeck config show [--target T]`                 — holodeck.config.show
//   - `meho holodeck pod list [--target T]`                    — holodeck.pod.list (JSONFlux)
//   - `meho holodeck pod info <pod-id> [--target T]`           — holodeck.pod.info
//   - `meho holodeck service list [--target T]`                — holodeck.service.list (JSONFlux)
//   - `meho holodeck k8s exec <kubectl-cmd> [--target T]`      — holodeck.k8s.exec (read-only)
//   - `meho holodeck logs tail <component> [--lines N] [--target T]` — holodeck.logs.tail
//   - `meho holodeck networking show [--target T]`             — holodeck.networking.show
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — the CLI alias verbs are pure
// operator ergonomics over the existing dispatcher surface (per
// CLAUDE.md postulate 5: agent surface stays narrow-waist meta-tools;
// vendor-specific tooling lives only in the CLI).
//
// The verb tree replaces the consumer's `scripts/holodeck.sh` wrapper
// (1:1 op mapping). The sister `scripts/clone-holodeck-instance.sh`
// wrapper — multi-step nested-lab bring-up — stays in place for v0.2
// and surfaces as a Runbook in a future Goal G11 once the runbook
// engine ships; see `docs/cross-repo/holodeck-onboarding.md`.
//
// The `meho holodeck` surface is operator-only — the MCP / agent
// surface continues to reach Holodeck ops via the narrow-waist
// search_operations + call_operation meta-tools (CLAUDE.md
// postulate 5). Each op's `llm_instructions.when_to_use` carries the
// canonical "Holodeck has no REST API; the underlying transport is
// PowerShell-over-SSH ..." note so agents know they're running on the
// appliance, not against a hosted API surface.
//
// IMPORTANT: the `holodeck.k8s.exec` CLI verb passes the operator's
// `kubectl` command verbatim into `params.command` of the typed op;
// the read-only safelist + shell-metacharacter guard live on the
// backend handler (see
// `backend/src/meho_backplane/connectors/holodeck/ops_read.py::parse_kubectl_command`).
// The CLI does NOT pre-parse, pre-validate, or sanitise — that would
// duplicate (and risk drifting from) the authoritative gate. Forward
// the raw string and let the backend refuse it if the verb is
// mutating or the input contains `;` / `&&` / `|` / `$(...)` /
// backticks / `>` / `<` / newline.
package holodeck

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
// `meho holodeck ...` dispatches against. Exported so the per-verb
// files and tests reference the same constant; a future re-versioning
// (holodeck-9.1 or holodeck-10.x) lands as a single line edit here.
//
// The id encodes the registry-v2 natural key triple
// "(product="holodeck", version="9.0", impl_id="holodeck-ssh")" per
// the connector_id parser convention in
// `backend/src/meho_backplane/operations/_lookup.py::parse_connector_id`:
// the trailing `-9.0` segment is the version; everything before is the
// impl_id (`holodeck-ssh`); the product is the first hyphen segment of
// the impl_id (`holodeck`). The impl_id discriminator (`-ssh`) leaves
// room for a future non-SSH control surface without breaking the
// resolver's tie-break ladder — relevant because Holodeck currently
// has no REST API but a vendor could add one in a future Toolkit
// release.
const ConnectorID = "holodeck-ssh-9.0"

// NewRootCmd returns the `meho holodeck` parent command. cmd/root.go
// grafts this onto the top-level command tree alongside the other
// built-in verb trees. The parent itself takes no args and prints its
// own help; every piece of behaviour lives in the per-subcommand RunE
// closures.
//
// Sub-tree layout follows the issue #855 verb table:
//   - `holodeck about`                    — flat verb
//   - `holodeck config <show>`            — sub-tree
//   - `holodeck pod <list|info>`          — sub-tree
//   - `holodeck service <list>`           — sub-tree
//   - `holodeck k8s <exec>`               — sub-tree (read-only)
//   - `holodeck logs <tail>`              — sub-tree
//   - `holodeck networking <show>`        — sub-tree
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "holodeck",
		Short: "Pre-scoped CLI verbs for the holodeck-ssh-9.0 connector",
		Long: "holodeck is the operator-facing verb tree for the holodeck-ssh-9.0\n" +
			"connector (registry triple (product=\"holodeck\", version=\"9.0\",\n" +
			"impl_id=\"holodeck-ssh\")). Each verb dispatches through\n" +
			"POST /api/v1/operations/call with connector_id=\"holodeck-ssh-9.0\"\n" +
			"pre-baked so operators don't type the connector ID on every\n" +
			"command. All shipped ops are read-only; write ops are out of\n" +
			"scope for G3.8 (#371).\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.\n\n" +
			"Transport note: Holodeck exposes no REST API. The underlying\n" +
			"transport is PowerShell-over-SSH (pwsh -EncodedCommand routed\n" +
			"through asyncssh) for cmdlet ops, plain SSH for kubectl /\n" +
			"shell-pipeline ops. The CLI does not see this directly — it\n" +
			"POSTs JSON to the backplane — but the agent-facing\n" +
			"llm_instructions on each op surface this transport so an LLM\n" +
			"doesn't compose against a non-existent REST surface.\n\n" +
			"Wrapper-retirement note: this verb tree replaces the\n" +
			"consumer's scripts/holodeck.sh wrapper 1:1. The sister\n" +
			"scripts/clone-holodeck-instance.sh wrapper (multi-step\n" +
			"nested-lab bring-up) stays in place for v0.2 and surfaces as\n" +
			"a Runbook in a future Goal G11 once the runbook engine ships;\n" +
			"see docs/cross-repo/holodeck-onboarding.md.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newConfigCmd())
	cmd.AddCommand(newPodCmd())
	cmd.AddCommand(newServiceCmd())
	cmd.AddCommand(newK8sCmd())
	cmd.AddCommand(newLogsCmd())
	cmd.AddCommand(newNetworkingCmd())
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

// resolveBackplane mirrors the bind9 / pfsense / k8s sibling helpers:
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
// output.StructuredError category. Identical routing to the bind9 /
// pfsense / k8s siblings: missing-config → auth_expired; everything
// else → unexpected.
func classifyBackplaneError(err error) *output.StructuredError {
	if errors.Is(err, auth.ErrConfigNotFound) {
		return output.AuthExpired(err.Error())
	}
	return output.Unexpected(err.Error())
}

// normaliseURL strips trailing slashes + parses the URL to fail fast
// on garbage input. Mirrors the bind9 / pfsense / k8s siblings.
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
// as the pfsense sibling: token-not-found / no-refresh-token →
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
// UTF-8 in pod names or service descriptions survives without
// producing an invalid UTF-8 cut. Same implementation as the pfsense
// / bind9 / k8s siblings.
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

// splitLines splits a string by newlines without the overhead of bufio.
// Used by the multi-line renderers (config.show, logs.tail) to cap
// human output at a sensible line count.
func splitLines(s string) []string {
	if s == "" {
		return nil
	}
	out := make([]string, 0, 32)
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
