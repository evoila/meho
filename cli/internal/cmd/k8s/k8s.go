// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

// Package k8s hosts the cobra commands under `meho k8s ...` for
// G3.2-T6 (#326) of Initiative #320. v0.2 ships the operator-facing
// alias verbs covering the 14 read ops the K8s typed connector
// registers, each pre-baking `connector_id="k8s-1.x"` so operators
// don't type the connector ID on every dispatch:
//
//	meho k8s about                                              — k8s.about
//	meho k8s ls [path]                                          — k8s.ls
//	meho k8s namespace list                                     — k8s.namespace.list
//	meho k8s node list                                          — k8s.node.list
//	meho k8s pod list [--namespace X | --all-namespaces] …      — k8s.pod.list
//	meho k8s pod info <name> --namespace X                      — k8s.pod.info
//	meho k8s deployment list [--namespace X | --all-namespaces] — k8s.deployment.list
//	meho k8s deployment info <name> --namespace X               — k8s.deployment.info
//	meho k8s service list --namespace X                         — k8s.service.list
//	meho k8s ingress list --namespace X                         — k8s.ingress.list
//	meho k8s configmap list --namespace X                       — k8s.configmap.list
//	meho k8s configmap info <name> --namespace X                — k8s.configmap.info
//	meho k8s event list --namespace X [--field-selector S]      — k8s.event.list
//	meho k8s logs <pod> --namespace X [--container Y] …         — k8s.logs
//
// Every verb is a thin Cobra command that POSTs to
// `/api/v1/operations/call` with a pre-baked connector_id. No new
// backend code; no new HTTP routes — CLI alias verbs are pure operator
// ergonomics over the existing dispatcher surface (per CLAUDE.md
// postulate 5: agent surface stays narrow-waist meta-tools; vendor-
// specific tooling lives only in the CLI). The underlying typed ops
// register via G3.2-T1..T5; this verb tree is the operator front-end
// over the same auth/policy/audit path the agent surface uses.
//
// `meho k8s pod list --target rke2-meho --namespace argocd` replaces
// the consumer's `kubectl-vcf.sh -n argocd get pods` invocation.
//
// Sibling-of-vault. The structure of this package mirrors
// `cli/internal/cmd/vault/` deliberately so a fix or pattern landed in
// one applies to the other with minimal translation. Helper functions
// (resolveBackplane / classifyBackplaneError / renderRequestError /
// doAuthedRequest / sendRequest / loadJSONFlag / truncate) are
// duplicated rather than shared because `cli/internal/cmd/vault` and
// `cli/internal/cmd/k8s` can't import each other without an import
// cycle (cmd/root.go grafts both onto the tree).
package k8s

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// ConnectorID is the pre-baked connector_id every verb under
// `meho k8s ...` dispatches against. Exported so the per-verb files
// and tests reference the same constant; a future re-versioning
// (k8s-2.x) lands as a single line edit here. The string form is the
// dispatcher's natural-key encoding (product="k8s", version="1.x",
// impl_id="k8s" → "k8s-1.x"), aligned with the backend connector's
// registration after the G3.2-T6 precursor substrate fix.
const ConnectorID = "k8s-1.x"

// NewRootCmd returns the `meho k8s` parent command. cmd/root.go grafts
// this onto the top-level command tree alongside the other built-in
// verb trees (operation / connector / targets / kb / retrieval /
// audit / vmware / vault). The parent itself takes no args and prints
// its own help; every piece of behaviour lives in the per-subcommand
// RunE closures.
//
// Sub-tree layout follows the K8s op groupings (Initiative #320 §5):
//
//	k8s about / ls                                       — top-level discovery verbs
//	k8s namespace <list>                                 — cluster inventory
//	k8s node <list>                                      — cluster inventory
//	k8s pod <list|info>                                  — workload
//	k8s deployment <list|info>                           — workload
//	k8s service <list>                                   — network
//	k8s ingress <list>                                   — network
//	k8s configmap <list|info>                            — config
//	k8s event <list>                                     — observability
//	k8s logs <pod>                                       — observability
//
// Sub-tree roots delegate to their own factories in this package so
// each noun's verbs live next to their tests.
func NewRootCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "k8s",
		Short: "Pre-scoped CLI verbs for the k8s-1.x connector",
		Long: "k8s is the operator-facing verb tree for the k8s-1.x\n" +
			"connector. Each verb dispatches through POST /api/v1/operations/call\n" +
			"with connector_id=\"k8s-1.x\" pre-baked so operators don't type\n" +
			"the connector ID on every command. Replaces the consumer's daily\n" +
			"`kubectl-vcf.sh` wrapper for the read-only workflows the operator\n" +
			"runs dozens of times per ticket (inventory, workload inspection,\n" +
			"log fetching).\n\n" +
			"Per CLAUDE.md postulate 5, these alias verbs are operator-only\n" +
			"ergonomics — they are not mirrored on the MCP surface. Agents\n" +
			"continue to use search_operations / call_operation against the\n" +
			"narrow-waist meta-tool contract.",
		SilenceUsage: true,
	}
	cmd.AddCommand(newAboutCmd())
	cmd.AddCommand(newLsCmd())
	cmd.AddCommand(newNamespaceCmd())
	cmd.AddCommand(newNodeCmd())
	cmd.AddCommand(newPodCmd())
	cmd.AddCommand(newDeploymentCmd())
	cmd.AddCommand(newServiceCmd())
	cmd.AddCommand(newIngressCmd())
	cmd.AddCommand(newConfigmapCmd())
	cmd.AddCommand(newEventCmd())
	cmd.AddCommand(newLogsCmd())
	return cmd
}

// renderRequestError translates an error from doAuthedRequest into the
// right output.RenderError category. Same classification ladder as the
// vault sibling.
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

// loadJSONFlag parses a flag value that is either inline JSON or an
// `@<file>` reference. Returns nil for an empty value so the caller
// can omit the key. Same shape as the vault sibling.
func loadJSONFlag(val string) (map[string]any, error) {
	if val == "" {
		return nil, nil
	}
	var raw []byte
	if strings.HasPrefix(val, "@") {
		path := strings.TrimPrefix(val, "@")
		var err error
		raw, err = os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read JSON file %q: %w", path, err)
		}
	} else {
		raw = []byte(val)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, fmt.Errorf("parse JSON: %w", err)
	}
	return m, nil
}

// truncate cuts s to maxLen runes, appending an ellipsis when
// truncation happened. Operates on runes (not bytes) so multi-byte
// UTF-8 in K8s-side names survives without producing an invalid
// UTF-8 cut. Same implementation as the vault sibling.
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
