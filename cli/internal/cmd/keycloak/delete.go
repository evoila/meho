// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/dispatch"
	"github.com/evoila/meho/cli/internal/output"
)

// The governed delete verbs (#3944):
//   - keycloak user delete   (--id|--username)                   → keycloak.user.delete
//   - keycloak group delete  (--id|--name) [--delete-subgroups]  → keycloak.group.delete
//   - keycloak client delete (--id|--client-id)                  → keycloak.client.delete
//   - keycloak realm delete  --realm R                           → keycloak.realm.delete (#3886)
//
// The four ops are on the destructive risk level. The backplane accepts
// such a call only with the preview_hash of a preview of the same op,
// target and params, and then the request waits until a second person
// approves it. The API and MCP work the same way (preview_operation, then
// call_operation with the hash). So each verb has two steps:
//
//  1. Without --preview-hash: preview only. Nothing is sent to Keycloak and
//     nothing waits for approval. The verb prints the preview_hash and tells
//     the operator to run the same command again with it.
//  2. With --preview-hash: ask for approval. The request waits until a
//     second person approves; approving it runs the delete.
//
// Refusals (a name with more than one match, a built-in client, the
// target's own login client or user, a group with sub-groups) come back in
// step 2 as status=denied with the reason, before anything waits.

// deleteVerb describes one of the three delete verbs.
type deleteVerb struct {
	noun      string // user | group | client
	opID      string
	nameFlag  string // the flag that takes the human name
	nameParam string // the op param that flag fills
	nameHelp  string
	example   string
}

func newUserDeleteCmd() *cobra.Command {
	return newDeleteCmd(deleteVerb{
		noun:      "user",
		opID:      "keycloak.user.delete",
		nameFlag:  "username",
		nameParam: "username",
		nameHelp:  "the exact username (used when --id is absent)",
		example: "  meho keycloak user delete --target kc --username event-guest\n" +
			"  meho keycloak user delete --target kc --username event-guest --preview-hash <hash>",
	})
}

func newGroupDeleteCmd() *cobra.Command {
	return newDeleteCmd(deleteVerb{
		noun:      "group",
		opID:      "keycloak.group.delete",
		nameFlag:  "name",
		nameParam: "name",
		nameHelp:  "the exact group name, searched at every level (used when --id is absent)",
		example: "  meho keycloak group delete --target kc --name event-team\n" +
			"  meho keycloak group delete --target kc --name event-team --preview-hash <hash>",
	})
}

func newClientDeleteCmd() *cobra.Command {
	return newDeleteCmd(deleteVerb{
		noun:      "client",
		opID:      "keycloak.client.delete",
		nameFlag:  "client-id",
		nameParam: "client_id",
		nameHelp:  "the exact clientId, the client's name (used when --id is absent)",
		example: "  meho keycloak client delete --target kc --client-id event-app\n" +
			"  meho keycloak client delete --target kc --client-id event-app --preview-hash <hash>",
	})
}

func newDeleteCmd(v deleteVerb) *cobra.Command {
	var (
		f               writeFlags
		objectID        string
		name            string
		previewHash     string
		deleteSubgroups bool
	)
	cmd := &cobra.Command{
		Use:   "delete",
		Short: fmt.Sprintf("Delete a Keycloak %s (destructive, a second person must approve)", v.noun),
		Long: fmt.Sprintf("delete dispatches %s. It removes one %s for good.\n\n", v.opID, v.noun) +
			"It runs in two steps:\n" +
			"  1. Without --preview-hash it only previews: nothing is deleted and\n" +
			"     nothing waits for approval. It prints a preview_hash.\n" +
			"  2. Run the same command again with --preview-hash <hash>. The request\n" +
			"     then waits until a second person approves it. Approving it runs\n" +
			"     the delete; do not run the command again after approval.\n\n" +
			fmt.Sprintf("Pass --id (the internal UUID) or --%s. A name that matches\n", v.nameFlag) +
			"more than one object is refused. If the object is already gone, the\n" +
			"result says already_absent=true. deleted=true is reported only after\n" +
			"a read of the object answers 404.\n\n" +
			"Exit codes: 0=ok/previewed/waiting for approval, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected.",
		Example:       v.example,
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if objectID == "" && name == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected(fmt.Sprintf("one of --id or --%s is required", v.nameFlag)), f.jsonOut)
			}
			params := map[string]any{}
			if objectID != "" {
				params["id"] = objectID
			}
			if name != "" {
				params[v.nameParam] = name
			}
			if deleteSubgroups {
				params["delete_subgroups"] = true
			}
			return runDelete(cmd, v.opID, f, params, previewHash)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&objectID, "id", "", fmt.Sprintf("the %s's internal UUID", v.noun))
	cmd.Flags().StringVar(&name, v.nameFlag, "", v.nameHelp)
	cmd.Flags().StringVar(&previewHash, "preview-hash", "",
		"the preview_hash from a first run of this command; with it, the delete waits for approval")
	if v.noun == "group" {
		cmd.Flags().BoolVar(&deleteSubgroups, "delete-subgroups", false,
			"also delete the group's sub-groups (a group with sub-groups is refused without this flag)")
	}
	return cmd
}

// runDelete previews the delete (no --preview-hash) or asks for approval
// (with --preview-hash).
func runDelete(
	cmd *cobra.Command,
	opID string,
	f writeFlags,
	params map[string]any,
	previewHash string,
) error {
	backplaneURL, err := resolveBackplane(f.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), f.jsonOut)
	}
	if previewHash == "" {
		p, err := conn.Preview(cmd.Context(), backplaneURL, opID, f.targetName, params)
		if err != nil {
			return renderRequestError(cmd, backplaneURL, err, f.jsonOut)
		}
		return renderDeletePreview(cmd, opID, p, f.jsonOut)
	}
	r, err := conn.CallWithPreviewHash(cmd.Context(), backplaneURL, opID, f.targetName, params, previewHash)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, f.jsonOut)
	}
	return renderCallResult(cmd, opID, r, f.jsonOut, printWriteResult(opID))
}

// renderDeletePreview prints step 1: the preview_hash and what to run next.
// status=ok exits 0; status=error / unavailable exits 1 with the reason.
func renderDeletePreview(cmd *cobra.Command, opID string, p *dispatch.PreviewResult, jsonOut bool) error {
	switch p.Status {
	case "ok", "error", "unavailable":
	default:
		return output.RenderError(cmd.ErrOrStderr(),
			output.Unexpected(fmt.Sprintf(
				"backplane returned invalid preview status %q (expected one of: ok / error / unavailable)",
				p.Status)),
			jsonOut)
	}
	if jsonOut {
		if err := output.PrintJSON(cmd.OutOrStdout(), p.Raw); err != nil {
			return err
		}
	} else {
		w := cmd.OutOrStdout()
		fmt.Fprintf(w, "%s %s — preview status=%s\n", ConnectorID, opID, p.Status)
		if p.Status == "ok" {
			fmt.Fprintln(w, "  Nothing was deleted, and nothing waits for approval yet.")
			fmt.Fprintf(w, "  preview_hash: %s\n", p.PreviewHash)
			fmt.Fprintf(w, "  To ask for approval, run the same command again and add: --preview-hash %s\n",
				p.PreviewHash)
		} else {
			if p.Error != nil && *p.Error != "" {
				fmt.Fprintf(w, "meho: preview %s: %s\n", p.Status, *p.Error)
			}
			if len(p.Extras) > 0 && string(p.Extras) != "null" {
				fmt.Fprintln(w, "extras:")
				fallbackResultRender(w, &CallResult{Result: p.Extras})
			}
		}
	}
	if p.Status == "ok" {
		return nil
	}
	return dispatch.ErrOpError
}

// newRealmDeleteCmd returns `meho keycloak realm delete` (keycloak.realm.delete,
// #3886). It removes a whole realm, with every user, client and group in it.
// --realm is required: the verb never falls back to the target's managed
// realm. It runs in the same two steps as the other delete verbs.
func newRealmDeleteCmd() *cobra.Command {
	var (
		f           writeFlags
		realmName   string
		previewHash string
	)
	cmd := &cobra.Command{
		Use:   "delete",
		Short: "Delete a whole Keycloak realm (destructive, a second person must approve)",
		Long: "delete dispatches keycloak.realm.delete. It removes the realm for good,\n" +
			"with every user, client, group and session in it.\n\n" +
			"It runs in two steps:\n" +
			"  1. Without --preview-hash it only previews: nothing is deleted and\n" +
			"     nothing waits for approval. It prints a preview_hash.\n" +
			"  2. Run the same command again with --preview-hash <hash>. The request\n" +
			"     then waits until a second person approves it. Approving it runs\n" +
			"     the delete; do not run the command again after approval.\n\n" +
			"--realm is required. MEHO refuses the target's admin realm, its managed\n" +
			"realm, and the realm MEHO's own login uses. The login realm is refused\n" +
			"on every Keycloak target, even one that is a different Keycloak, because\n" +
			"MEHO cannot tell that for sure. To stop logins but keep the realm, use\n" +
			"`meho keycloak realm update` with enabled=false instead.\n" +
			"If the realm is already gone, the result says already_absent=true.\n" +
			"deleted=true is reported only after a read of the realm answers 404.\n\n" +
			"Exit codes: 0=ok/previewed/waiting for approval, 1=error/denied,\n" +
			"2=auth_expired, 3=unreachable, 4=unexpected.",
		Example: "  meho keycloak realm delete --target kc --realm test-run-7\n" +
			"  meho keycloak realm delete --target kc --realm test-run-7 --preview-hash <hash>",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if realmName == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("--realm is required"), f.jsonOut)
			}
			return runDelete(cmd, "keycloak.realm.delete", f,
				map[string]any{"realm": realmName}, previewHash)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&realmName, "realm", "", "the name of the realm to delete (required)")
	cmd.Flags().StringVar(&previewHash, "preview-hash", "",
		"the preview_hash from a first run of this command; with it, the delete waits for approval")
	return cmd
}
