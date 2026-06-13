// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// G3.13-T4 (#1406) — the approval-gated write verbs under
// `meho keycloak ...`. Every write op registers requires_approval=True on
// the backplane, so a dispatch returns status=awaiting_approval until a
// human approves through the queue (G11.7-T1 #1401); the CLI surfaces that
// status verbatim. The verbs retire the consumer's five Keycloak bootstrap
// scripts (keycloak-bootstrap-meho-{admin,cli,mcp,web}.sh and
// keycloak-provision-meho-user.sh) — see
// docs/cross-repo/keycloak-onboarding.md for the script→op mapping.
//
// Verb tree (write half):
//   - keycloak realm create   --representation-file F     → keycloak.realm.create
//   - keycloak realm update   --representation-file F     → keycloak.realm.update
//   - keycloak client create  --representation-file F     → keycloak.client.create
//   - keycloak client update  (--id|--client-id) -f F     → keycloak.client.update
//   - keycloak client-scope create --representation-file F→ keycloak.client_scope.create
//   - keycloak protocol-mapper create (--id|--client-id) -f F → keycloak.protocol_mapper.create
//   - keycloak user create    -f F --password-secret-ref R→ keycloak.user.create
//   - keycloak user reset-password (--id|--username) --password-secret-ref R → keycloak.user.reset_password
//   - keycloak role-mapping assign (--id|--username) --role R [--role ...] → keycloak.role_mapping.assign
//
// The representation body is a JSON file (--representation-file / -f) so an
// operator can feed the same JSON a bootstrap script POSTed to kcadm.sh.
// The password for user create / reset-password is NEVER passed on the
// command line — only a Vault path (--password-secret-ref) — so it never
// lands in shell history, ps output, or the op params.

// loadRepresentation reads a JSON object from the file at path. Returns a
// structured error (mapped to exit code 4 / unexpected) when the file is
// unreadable or not a JSON object.
func loadRepresentation(path string) (map[string]any, *output.StructuredError) {
	raw, err := os.ReadFile(path) // #nosec G304 -- operator-supplied path, operator-only CLI
	if err != nil {
		return nil, output.Unexpected(fmt.Sprintf("read representation file %q: %v", path, err))
	}
	var rep map[string]any
	if err := json.Unmarshal(raw, &rep); err != nil {
		return nil, output.Unexpected(fmt.Sprintf("parse representation file %q as JSON object: %v", path, err))
	}
	return rep, nil
}

// printWriteResult is the shared pretty-printer for the value-free write
// confirmations every write op returns. It renders the op header then the
// flat result object's scalar fields (id / created / conflict / updated /
// password_reset / assigned_roles …) — none of which ever carry secret
// material. The awaiting_approval (parked) status never reaches this
// printer: the shared dispatch.Render intercepts it ahead of the
// pretty-printer and renders the parked hint itself (exit 0).
func printWriteResult(opID string) func(w io.Writer, r *CallResult) {
	return func(w io.Writer, r *CallResult) {
		fmt.Fprintf(w, "%s %s — status=%s (%.0fms)\n", ConnectorID, opID, r.Status, r.DurationMs)
		if r.Status != "ok" {
			printErrorTrailer(w, r)
			return
		}
		obj, err := decodeFlatObject(r.Result)
		if err != nil || obj == nil {
			fallbackResultRender(w, r)
			return
		}
		for _, key := range writeResultKeyOrder {
			if v, ok := obj[key]; ok && v != nil {
				fmt.Fprintf(w, "  %-16s %v\n", key+":", v)
			}
		}
	}
}

// writeResultKeyOrder pins a stable render order for the write
// confirmations' scalar fields so the output is diff-stable across ops.
var writeResultKeyOrder = []string{
	"realm", "name", "client_id", "client_uuid", "id", "username",
	"mapper_name", "created", "updated", "conflict", "password_reset",
	"assigned_roles",
}

// decodeFlatObject decodes the write confirmation envelope — a flat JSON
// object of scalar fields (and the assigned_roles array) — into a map.
func decodeFlatObject(raw json.RawMessage) (map[string]any, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var obj map[string]any
	if err := json.Unmarshal(raw, &obj); err != nil {
		return nil, fmt.Errorf("decode write result: %w", err)
	}
	return obj, nil
}

// dispatchWrite is the shared dispatch+render path for the write verbs:
// resolve the backplane, dispatch the op with the assembled params, and
// render the value-free confirmation.
func dispatchWrite(
	cmd *cobra.Command,
	opID, targetName string,
	params map[string]any,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printWriteResult(opID))
}

// writeFlags is the common flag bundle every write verb binds: --target,
// --json, --backplane. The per-verb commands add their own flags.
type writeFlags struct {
	targetName        string
	jsonOut           bool
	backplaneOverride string
}

func (f *writeFlags) bind(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&f.jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
}
