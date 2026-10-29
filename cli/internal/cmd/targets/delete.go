// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// deleteResult is the --json envelope for a delete that ran or that
// the operator declined.
type deleteResult struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Force  bool   `json:"force"`
}

// hasReferencesDetail is the 409 detail DELETE /api/v1/targets/{name}
// sends when topology nodes still point at the target.
type hasReferencesDetail struct {
	Kind          string `json:"kind"`
	GraphNodeRefs int    `json:"graph_node_refs"`
}

// newDeleteCmd returns the `meho targets delete` command.
//
// CLI shape:
//
//	meho targets delete <name|alias> [--force] [--confirm] [--json] [--backplane <url>]
//
// It sends one DELETE /api/v1/targets/{name} (`?force=true` with
// --force). The server soft-deletes: the row stays so audit rows still
// point at it, but no read path or call finds it any more. Without
// --confirm the command asks on stdin first; closed stdin counts as
// "no", so a script never deletes by accident.
//
// Exit codes: 0 deleted or declined; 4 for 404 (no such target), 409
// (ambiguous alias, or topology nodes still point at the target), and
// other server errors; 2 / 3 / 5 as for the other verbs.
func newDeleteCmd() *cobra.Command {
	var (
		force             bool
		confirm           bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "delete <name|alias>",
		Short: "Delete one target (the row is kept for the audit log)",
		Long: "delete calls DELETE /api/v1/targets/{name}. MEHO stops using the " +
			"target, but keeps its row so the audit log still points at it. " +
			"The system itself and its Vault secret are not touched.\n\n" +
			"When topology nodes still point at the target, the server " +
			"refuses and the command says how many. `--force` deletes it " +
			"anyway: the nodes stay, but they point at a deleted target.\n\n" +
			"Without --confirm the command asks for y/N on stdin. Closed " +
			"stdin counts as no, so scripts must pass --confirm. Declining " +
			"exits 0 without calling the server.\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runDelete(cmd, args[0], force, confirm, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().BoolVar(&force, "force", false,
		"delete even when topology nodes point at the target (the nodes stay)")
	cmd.Flags().BoolVar(&confirm, "confirm", false, "skip the stdin confirmation prompt")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit a machine-readable envelope instead of the human line")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL the target lives in (defaults to the URL recorded by `meho login`)")
	return cmd
}

// runDelete asks for confirmation, sends the DELETE, and renders the
// result or the error.
func runDelete(cmd *cobra.Command, name string, force, confirm, jsonOut bool, backplaneOverride string) error {
	result := deleteResult{Name: name, Force: force}
	// Ask before resolving the backplane, so a decline exits 0 even
	// when the operator is not logged in. With --json the prompt goes
	// to stderr, so stdout holds only the envelope.
	promptW := cmd.OutOrStdout()
	if jsonOut {
		promptW = cmd.ErrOrStderr()
	}
	if !confirm && !confirmPrompt(cmd, promptW, deletePrompt(name, force)) {
		result.Status = "declined"
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), result)
		}
		fmt.Fprintf(cmd.OutOrStdout(), "declined: target %q not deleted\n", name)
		return nil
	}
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	path := "/api/v1/targets/" + pathEscape(name)
	if force {
		path += "?force=true"
	}
	if _, err := doAuthedRequest(cmd.Context(), backplaneURL, http.MethodDelete, path, nil); err != nil {
		var he *httpError
		if errors.As(err, &he) && he.StatusCode == http.StatusConflict {
			if refs, ok := parseHasReferences(he.Body); ok {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(hasReferencesMessage(refs)), jsonOut)
			}
		}
		return renderImportRequestError(cmd, backplaneURL, err, jsonOut)
	}
	result.Status = "deleted"
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), result)
	}
	fmt.Fprintf(cmd.OutOrStdout(), "deleted target %q\n", name)
	return nil
}

// deletePrompt is the question shown before the DELETE.
func deletePrompt(name string, force bool) string {
	prompt := fmt.Sprintf("Delete target %q? MEHO stops using it; the row is kept for the audit log.", name)
	if force {
		prompt += " Topology nodes that point at it stay, but will point at a deleted target."
	}
	return prompt + " Continue?"
}

// parseHasReferences reads the `target_has_references` 409 detail.
// ok is false for any other 409 (for example an ambiguous alias), which
// the shared renderHTTPStatus ladder handles.
func parseHasReferences(body string) (int, bool) {
	var env detailEnvelope
	if err := json.Unmarshal([]byte(body), &env); err != nil {
		return 0, false
	}
	var d hasReferencesDetail
	if err := json.Unmarshal(env.Detail, &d); err != nil || d.Kind != "target_has_references" {
		return 0, false
	}
	return d.GraphNodeRefs, true
}

// hasReferencesMessage explains the 409 in plain words and names the
// way out.
func hasReferencesMessage(refs int) string {
	nodes := "topology nodes"
	if refs == 1 {
		nodes = "topology node"
	}
	return fmt.Sprintf("This target is linked from %d %s. The nodes stay, but they "+
		"will point at a deleted target. Run again with --force to delete it anyway.", refs, nodes)
}

// confirmPrompt writes prompt to promptW and returns true only when the
// operator answers y or yes on stdin. Any read error, such as EOF on
// closed stdin, counts as no. Same shape as the memory helper.
func confirmPrompt(cmd *cobra.Command, promptW io.Writer, prompt string) bool {
	fmt.Fprintf(promptW, "%s [y/N]: ", prompt)
	var answer string
	if _, err := fmt.Fscanln(cmd.InOrStdin(), &answer); err != nil {
		return false
	}
	answer = strings.ToLower(strings.TrimSpace(answer))
	return answer == "y" || answer == "yes"
}
