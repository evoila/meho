// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package keycloak

import (
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// Group-lifecycle verbs under `meho keycloak group ...` (#3280). The read
// verbs (`list`, `member list`) are safe; the write verbs (`create`,
// `update-attributes`, `member add`, `member remove`) are approval-gated —
// every write dispatches with requires_approval=True on the backplane, so
// the CLI surfaces status=awaiting_approval until a human approves.
//
// Groups are the backplane's tenant-claim primitive: a role group carries
// tenant_id / tenant_role as group attributes an aggregated attribute
// mapper mints into the token. `create` + `update-attributes` set those
// attributes; `member add` / `member remove` attach or detach a user.
//
// Verb tree:
//   - keycloak group list [--search S] [--parent-id UUID] [--attributes] [--max N] → keycloak.group.list
//   - keycloak group create --name N [--parent-id UUID] [--attribute k=v ...]      → keycloak.group.create
//   - keycloak group update-attributes (--id|--name) [--parent-id UUID]
//         [--attribute k=v ...] [--replace]                                        → keycloak.group.update_attributes
//   - keycloak group member add (--group-id|--group-name) (--user-id|--username)   → keycloak.group.member.add
//   - keycloak group member remove (--group-id|--group-name) (--user-id|--username)→ keycloak.group.member.remove
//   - keycloak group member list --id UUID [--max N]                               → keycloak.group.member.list
//
// Attributes are supplied as repeatable `--attribute key=value` flags;
// repeating a key accumulates its values into the Keycloak
// Map<String,List<String>> shape (e.g. --attribute tenant_id=t-2
// --attribute tenant_role=admin).

// newGroupCmd returns the `meho keycloak group` parent with the read verbs
// `list` / `member list` and the approval-gated write verbs `create`,
// `update-attributes`, `member add`, `member remove`.
func newGroupCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "group",
		Short:        "Keycloak group sub-verbs (list, create, update-attributes, member)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newGroupListCmd())
	cmd.AddCommand(newGroupCreateCmd())
	cmd.AddCommand(newGroupUpdateAttributesCmd())
	cmd.AddCommand(newGroupMemberCmd())
	return cmd
}

// parseAttributeFlags folds repeatable `key=value` flags into Keycloak's
// Map<String, []string> attribute shape; a repeated key accumulates its
// values in order. A flag without an `=` errors (mapped to exit code 4).
func parseAttributeFlags(pairs []string) (map[string]any, *output.StructuredError) {
	if len(pairs) == 0 {
		return nil, nil
	}
	attrs := map[string][]string{}
	order := []string{}
	for _, pair := range pairs {
		key, value, ok := strings.Cut(pair, "=")
		if !ok || key == "" {
			return nil, output.Unexpected(fmt.Sprintf(
				"invalid --attribute %q: expected key=value", pair))
		}
		if _, seen := attrs[key]; !seen {
			order = append(order, key)
		}
		attrs[key] = append(attrs[key], value)
	}
	out := make(map[string]any, len(order))
	for _, key := range order {
		vals := make([]any, len(attrs[key]))
		for i, v := range attrs[key] {
			vals[i] = v
		}
		out[key] = vals
	}
	return out, nil
}

// ---- read: group list -----------------------------------------------------

func newGroupListCmd() *cobra.Command {
	var (
		targetName        string
		search            string
		parentID          string
		attributes        bool
		maxResults        int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List Keycloak realm groups (optionally with attributes / by parent)",
		Long: "list dispatches keycloak.group.list and renders the groups as a\n" +
			"table of name / path / internal id. --search filters by name\n" +
			"substring; --parent-id lists that group's direct children;\n" +
			"--attributes includes each group's attributes (Keycloak's brief\n" +
			"projection omits them); --max caps the count. --json emits the full\n" +
			"OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak group list --target rdc-keycloak --attributes\n" +
			"  meho keycloak group list --target rdc-keycloak --search role- --json | jq '.result.rows[].id'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := resolveBackplane(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
			}
			params := map[string]any{}
			if search != "" {
				params["search"] = search
			}
			if parentID != "" {
				params["parent_id"] = parentID
			}
			if attributes {
				params["brief"] = false
			}
			if maxResults > 0 {
				params["max"] = maxResults
			}
			r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.group.list", targetName, params)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			return renderCallResult(cmd, "keycloak.group.list", r, jsonOut, printGroupList)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against (required)")
	cmd.Flags().StringVar(&search, "search", "", "filter by group name substring (Keycloak ?search=)")
	cmd.Flags().StringVar(&parentID, "parent-id", "", "list this group's direct children instead of top-level")
	cmd.Flags().BoolVar(&attributes, "attributes", false, "include group attributes (briefRepresentation=false)")
	cmd.Flags().IntVar(&maxResults, "max", 0, "cap on the number of groups returned (0 = no cap)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func printGroupList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.group.list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, total, err := decodeRowsResult(r.Result)
	if err != nil || rows == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-28s %-30s %s\n", "NAME", "PATH", "INTERNAL_ID")
	for _, row := range rows {
		name := truncate(stringField(row, "name"), 28)
		path := truncate(stringField(row, "path"), 30)
		fmt.Fprintf(w, "  %-28s %-30s %s\n", name, path, stringField(row, "id"))
	}
	fmt.Fprintf(w, "  (%d groups)\n", total)
}

// ---- write: group create --------------------------------------------------

func newGroupCreateCmd() *cobra.Command {
	var (
		f        writeFlags
		name     string
		parentID string
		attrs    []string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a realm group with attributes (approval-gated)",
		Long: "create dispatches keycloak.group.create. --name is required;\n" +
			"--parent-id nests the group under an existing one; repeatable\n" +
			"--attribute key=value flags set the group's attributes (e.g.\n" +
			"--attribute tenant_id=t-2 --attribute tenant_role=admin). Requires\n" +
			"approval; a 409 already-exists is idempotent (already_exists=true).\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak group create --target rdc-keycloak --name role-tenant-2 " +
			"--attribute tenant_id=t-2 --attribute tenant_role=admin",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			attributes, serr := parseAttributeFlags(attrs)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			params := map[string]any{"name": name}
			if parentID != "" {
				params["parent_id"] = parentID
			}
			if attributes != nil {
				params["attributes"] = attributes
			}
			return dispatchWrite(cmd, "keycloak.group.create", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&name, "name", "", "the group name (required)")
	cmd.Flags().StringVar(&parentID, "parent-id", "", "parent group UUID to nest under (omit for top-level)")
	cmd.Flags().StringArrayVar(&attrs, "attribute", nil, "group attribute as key=value (repeatable)")
	if err := cmd.MarkFlagRequired("name"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

// ---- write: group update-attributes ---------------------------------------

func newGroupUpdateAttributesCmd() *cobra.Command {
	var (
		f         writeFlags
		groupUUID string
		name      string
		parentID  string
		attrs     []string
		replace   bool
	)
	cmd := &cobra.Command{
		Use:   "update-attributes",
		Short: "Merge or replace a realm group's attributes (approval-gated)",
		Long: "update-attributes dispatches keycloak.group.update_attributes.\n" +
			"Keys on the group UUID (--id) or resolves --name (+ optional\n" +
			"--parent-id). Repeatable --attribute key=value flags are merged onto\n" +
			"the group's current attributes; --replace sets them wholesale.\n" +
			"Requires approval.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho keycloak group update-attributes --target rdc-keycloak --name role-tenant " +
			"--attribute tenant_role=admin",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if groupUUID == "" && name == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --id or --name is required"), f.jsonOut)
			}
			attributes, serr := parseAttributeFlags(attrs)
			if serr != nil {
				return output.RenderError(cmd.ErrOrStderr(), serr, f.jsonOut)
			}
			if attributes == nil {
				attributes = map[string]any{}
			}
			params := map[string]any{"attributes": attributes, "replace": replace}
			if groupUUID != "" {
				params["id"] = groupUUID
			}
			if name != "" {
				params["name"] = name
			}
			if parentID != "" {
				params["parent_id"] = parentID
			}
			return dispatchWrite(cmd, "keycloak.group.update_attributes", f.targetName,
				params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&groupUUID, "id", "", "the group's internal UUID (skips name resolution)")
	cmd.Flags().StringVar(&name, "name", "", "the group name (resolved to UUID when --id is absent)")
	cmd.Flags().StringVar(&parentID, "parent-id", "", "parent UUID to scope --name to a subgroup")
	cmd.Flags().StringArrayVar(&attrs, "attribute", nil, "group attribute as key=value (repeatable)")
	cmd.Flags().BoolVar(&replace, "replace", false, "replace the attribute map wholesale (default: merge)")
	return cmd
}

// ---- member sub-tree ------------------------------------------------------

func newGroupMemberCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "member",
		Short:        "Keycloak group membership sub-verbs (add, remove, list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newGroupMemberAddCmd())
	cmd.AddCommand(newGroupMemberRemoveCmd())
	cmd.AddCommand(newGroupMemberListCmd())
	return cmd
}

// groupMemberWriteCmd builds the shared add/remove membership verb — they
// differ only in the op_id and the verb noun.
func groupMemberWriteCmd(verb, opID string) *cobra.Command {
	var (
		f         writeFlags
		groupUUID string
		groupName string
		parentID  string
		userUUID  string
		username  string
	)
	cmd := &cobra.Command{
		Use:   verb,
		Short: fmt.Sprintf("%s a user %s a realm group (approval-gated)", verb, prep(verb)),
		Long: fmt.Sprintf("%s dispatches %s. Keys on the group UUID (--group-id) or\n"+
			"--group-name (+ optional --parent-id), and the user UUID (--user-id)\n"+
			"or --username. Idempotent: no-op membership returns unchanged=true.\n"+
			"Requires approval.\n\n"+
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n"+
			"3=unreachable, 4=unexpected.", verb, opID),
		Example: fmt.Sprintf(
			"  meho keycloak group member %s --target rdc-keycloak --group-name role-tenant --username operator-a",
			verb),
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			if groupUUID == "" && groupName == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --group-id or --group-name is required"), f.jsonOut)
			}
			if userUUID == "" && username == "" {
				return output.RenderError(cmd.ErrOrStderr(),
					output.Unexpected("one of --user-id or --username is required"), f.jsonOut)
			}
			params := map[string]any{}
			if groupUUID != "" {
				params["group_id"] = groupUUID
			}
			if groupName != "" {
				params["group_name"] = groupName
			}
			if parentID != "" {
				params["parent_id"] = parentID
			}
			if userUUID != "" {
				params["user_id"] = userUUID
			}
			if username != "" {
				params["username"] = username
			}
			return dispatchWrite(cmd, opID, f.targetName, params, f.jsonOut, f.backplaneOverride)
		},
	}
	f.bind(cmd)
	cmd.Flags().StringVar(&groupUUID, "group-id", "", "the group's internal UUID")
	cmd.Flags().StringVar(&groupName, "group-name", "", "the group name (resolved when --group-id is absent)")
	cmd.Flags().StringVar(&parentID, "parent-id", "", "parent UUID to scope --group-name to a subgroup")
	cmd.Flags().StringVar(&userUUID, "user-id", "", "the user's internal UUID")
	cmd.Flags().StringVar(&username, "username", "", "the username (resolved when --user-id is absent)")
	return cmd
}

func newGroupMemberAddCmd() *cobra.Command {
	return groupMemberWriteCmd("add", "keycloak.group.member.add")
}

func newGroupMemberRemoveCmd() *cobra.Command {
	return groupMemberWriteCmd("remove", "keycloak.group.member.remove")
}

// prep renders the preposition for the member verb's short help.
func prep(verb string) string {
	if verb == "remove" {
		return "from"
	}
	return "to"
}

func newGroupMemberListCmd() *cobra.Command {
	var (
		targetName        string
		groupUUID         string
		maxResults        int
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List the members of a Keycloak group by internal UUID (no credentials)",
		Long: "list dispatches keycloak.group.member.list for the group whose\n" +
			"internal UUID is --id (from `meho keycloak group list`). Renders the\n" +
			"member users as username / internal id; --max caps the count.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example:       "  meho keycloak group member list --target rdc-keycloak --id 77777777-7777-7777-7777-777777777777",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			backplaneURL, err := resolveBackplane(backplaneOverride)
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
			}
			params := map[string]any{"id": groupUUID}
			if maxResults > 0 {
				params["max"] = maxResults
			}
			r, err := dispatchOp(cmd.Context(), backplaneURL, "keycloak.group.member.list", targetName, params)
			if err != nil {
				return renderRequestError(cmd, backplaneURL, err, jsonOut)
			}
			return renderCallResult(cmd, "keycloak.group.member.list", r, jsonOut, printGroupMemberList)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against (required)")
	cmd.Flags().StringVar(&groupUUID, "id", "", "the group's internal UUID (from `meho keycloak group list`) (required)")
	cmd.Flags().IntVar(&maxResults, "max", 0, "cap on the number of members returned (0 = no cap)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	if err := cmd.MarkFlagRequired("id"); err != nil {
		panic(err) // programmer error: the flag is defined directly above
	}
	return cmd
}

func printGroupMemberList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s keycloak.group.member.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, total, err := decodeRowsResult(r.Result)
	if err != nil || rows == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-30s %s\n", "USERNAME", "INTERNAL_ID")
	for _, row := range rows {
		fmt.Fprintf(w, "  %-30s %s\n", truncate(stringField(row, "username"), 30), stringField(row, "id"))
	}
	fmt.Fprintf(w, "  (%d members)\n", total)
}
