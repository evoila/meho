// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package secret

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// opMove is the canonical op_id `meho secret move` dispatches.
const opMove = "secret.move"

// statusAwaitingApproval is the change-class park status the policy gate
// returns for an unapproved move. The shared dispatch.Render rejects any
// status outside ok/error/denied as an exit-4 "invalid OperationResult
// status", so this verb intercepts awaiting_approval BEFORE delegating to
// conn.Render (option (b) of #1580 — a package-local render path, leaving
// the shared status enum and every other vendor verb untouched).
const statusAwaitingApproval = "awaiting_approval"

// newMoveCmd returns the `meho secret move` command (secret.move —
// change-class, approval-gated). It dispatches secret.move with the
// source/sink references and the audit reason as opaque params; the
// secret value is never passed inline.
func newMoveCmd() *cobra.Command {
	var (
		from      string
		to        string
		reason    string
		jsonOut   bool
		backplane string
	)
	cmd := &cobra.Command{
		Use:   "move",
		Short: "Move a credential between stores server-side (references only, approval-gated)",
		Long: "move dispatches secret.move: the backplane reads the credential\n" +
			"named by --from, transfers it, and re-writes it to --to entirely\n" +
			"server-side. --from and --to are '<kind>:<ref>' references (e.g.\n" +
			"'vault:secret/db/prod#password'); 'kind' selects the store adapter\n" +
			"and 'ref' is the store-specific address. --reason is recorded for\n" +
			"the approver and the audit trail.\n\n" +
			"References, not values: there is NO inline-secret flag of any kind.\n" +
			"The value is never passed on the command line, so it never lands in\n" +
			"shell history, ps output, or the op params. The response returns\n" +
			"only the move status, the value's SHA-256, and its byte length —\n" +
			"never the value.\n\n" +
			"Change-class: the move requires approval. An unapproved dispatch\n" +
			"returns status=awaiting_approval (parked for a human to approve\n" +
			"through the approval queue); re-dispatch after approval.\n\n" +
			"Exit codes: 0=ok/awaiting_approval, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho secret move --from vault:secret/db/prod#password " +
			"--to vault:secret/db/standby#password --reason 'provision standby DB'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runMove(cmd, from, to, reason, jsonOut, backplane)
		},
	}
	// References-not-values: --from / --to carry only opaque '<kind>:<ref>'
	// strings, --reason an audit justification. There is deliberately NO
	// --value / --secret / --password flag.
	cmd.Flags().StringVar(&from, "from", "",
		"source '<kind>:<ref>' reference the credential is read from (required)")
	cmd.Flags().StringVar(&to, "to", "",
		"sink '<kind>:<ref>' reference the credential is written to (required)")
	cmd.Flags().StringVar(&reason, "reason", "",
		"justification recorded for the approver and the audit trail (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplane, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	for _, name := range []string{"from", "to", "reason"} {
		if err := cmd.MarkFlagRequired(name); err != nil {
			panic(err) // programmer error: the flag is defined directly above
		}
	}
	return cmd
}

// runMove resolves the backplane, dispatches secret.move with the
// reference/reason params, and renders the value-free result.
func runMove(
	cmd *cobra.Command,
	from, to, reason string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	// Only references and the reason ever cross the wire — never a value.
	params := map[string]any{
		"from":   from,
		"to":     to,
		"reason": reason,
	}
	r, err := conn.Call(cmd.Context(), backplaneURL, opMove, "", params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderMoveResult(cmd, r, jsonOut)
}

// renderMoveResult surfaces the secret.move result. awaiting_approval is
// handled package-locally (the shared conn.Render would exit-4 it as an
// invalid status); every other status delegates to conn.Render so the
// ok→exit-0 / error|denied→exit-1 classification and the JSON envelope
// path are reused unchanged.
func renderMoveResult(cmd *cobra.Command, r *dispatch.CallResult, jsonOut bool) error {
	if r.Status == statusAwaitingApproval {
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), r)
		}
		printMoveResult(cmd.OutOrStdout(), r)
		return nil // parked, not failed — exit 0.
	}
	return conn.Render(cmd, opMove, r, jsonOut, printMoveResult)
}

// printMoveResult renders the value-free move confirmation: the op header
// then, on success, only the move status / value SHA-256 / byte length.
// It never renders a secret value because the response never carries one.
func printMoveResult(w io.Writer, r *dispatch.CallResult) {
	fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opMove, r.Status, r.DurationMs)
	if r.Status == statusAwaitingApproval {
		fmt.Fprintln(w, "  parked for human approval — approve via the approval queue, then re-dispatch")
		return
	}
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	obj, err := decodeMoveResult(r.Result)
	if err != nil || obj == nil {
		// Fall back to the raw result envelope (still value-free — the op
		// response schema carries only status / value_sha256 / length).
		if len(r.Result) > 0 && string(r.Result) != "null" {
			if pretty, perr := dispatch.PrettyJSON(r.Result); perr == nil {
				fmt.Fprintln(w, pretty)
			}
		}
		return
	}
	for _, key := range moveResultKeyOrder {
		if v, ok := obj[key]; ok && v != nil {
			fmt.Fprintf(w, "  %-13s %v\n", key+":", v)
		}
	}
}

// moveResultKeyOrder pins a stable render order for the move
// confirmation's scalar fields so the output is diff-stable. These are
// the only fields secret.move returns; none carries the value.
var moveResultKeyOrder = []string{"status", "value_sha256", "length"}

// decodeMoveResult decodes the move confirmation envelope — a flat JSON
// object of scalar fields ({status, value_sha256, length}) — into a map.
func decodeMoveResult(raw json.RawMessage) (map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var obj map[string]any
	if err := json.Unmarshal(raw, &obj); err != nil {
		return nil, fmt.Errorf("decode move result: %w", err)
	}
	return obj, nil
}

// printErrorTrailer surfaces the dispatcher error + extras envelope on the
// non-ok branch. Mirrors the keycloak sibling helper.
func printErrorTrailer(w io.Writer, r *dispatch.CallResult) {
	if r.Error != nil && *r.Error != "" {
		fmt.Fprintf(w, "meho: connector error: %s\n", *r.Error)
	} else {
		fmt.Fprintf(w, "meho: connector status=%s\n", r.Status)
	}
	if len(r.Extras) > 0 && string(r.Extras) != "null" {
		fmt.Fprintln(w, "extras:")
		if pretty, err := dispatch.PrettyJSON(r.Extras); err == nil {
			fmt.Fprintln(w, pretty)
		} else {
			fmt.Fprintln(w, string(r.Extras))
		}
	}
}
