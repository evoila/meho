// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vault

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// KV-v2 op IDs registered by G3.3-T1 (#545). The CLI references the
// canonical op_id strings verbatim — a drift here would dispatch a
// non-existent op (the dispatcher would return status=error
// "operation not found", which surfaces in the standard trailer).
const (
	opKVRead     = "vault.kv.read"
	opKVList     = "vault.kv.list"
	opKVPut      = "vault.kv.put"
	opKVVersions = "vault.kv.versions"
	opKVDelete   = "vault.kv.delete"
)

// newKVCmd returns the `meho vault kv` parent command and assembles
// its five verbs. The parent itself takes no args and prints its own
// help.
func newKVCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "kv",
		Short:        "KV-v2 secret verbs (read / list / put / versions / delete)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newKVReadCmd())
	cmd.AddCommand(newKVListCmd())
	cmd.AddCommand(newKVPutCmd())
	cmd.AddCommand(newKVVersionsCmd())
	cmd.AddCommand(newKVDeleteCmd())
	return cmd
}

// kvAddrFlags holds the flags every KV verb shares (target slug,
// --json, --backplane). Each verb embeds it so the flag wiring + the
// resolve-and-dispatch boilerplate stays in one place.
type kvAddrFlags struct {
	targetName        string
	jsonOut           bool
	backplaneOverride string
}

// bindKVAddrFlags registers the shared address flags on a command and
// returns a pointer to the populated struct. Verbs add their own
// op-specific flags (--data, --cas, --versions) on top.
func bindKVAddrFlags(cmd *cobra.Command) *kvAddrFlags {
	f := &kvAddrFlags{}
	cmd.Flags().StringVar(&f.targetName, "target", "",
		"Vault target slug to dispatch against (resolved server-side)")
	cmd.Flags().BoolVar(&f.jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON instead of the human render")
	cmd.Flags().StringVar(&f.backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return f
}

// dispatchKV is the resolve-backplane → dispatch → render pipeline
// every KV verb runs. opID + params come from the verb; the shared
// address flags carry target / json / backplane. The generic renderer
// is used: Vault payloads (secret data, metadata, version maps) are
// nested JSON the operator reads as a tree, and set-shaped responses
// (kv list) arrive already reduced to the JSONFlux sample + handle by
// the dispatcher.
func dispatchKV(cmd *cobra.Command, f *kvAddrFlags, opID string, params map[string]any) error {
	backplaneURL, err := resolveBackplane(f.backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), f.jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, f.targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, f.jsonOut)
	}
	return renderCallResult(cmd, opID, r, f.jsonOut, nil)
}

// kvPathParams folds the `<mount> <path>` positional pair into the op
// params map. The G3.3-T1 KV-v2 handlers take `path` (required) and an
// optional `mount` (defaulting to "secret" server-side). The CLI
// always sends `mount` explicitly so the operator's positional choice
// is authoritative — there is no client-side mount default that could
// drift from the handler's.
func kvPathParams(mount, path string) map[string]any {
	return map[string]any{"mount": mount, "path": path}
}

// newKVReadCmd returns `meho vault kv read <mount> <path>`.
func newKVReadCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "read <mount> <path>",
		Short: "Read the latest version of a KV-v2 secret",
		Long: "read dispatches op_id=\"vault.kv.read\" against connector_id=\n" +
			"\"vault-1.x\". <mount> is the KV-v2 engine mount (e.g. secret);\n" +
			"<path> is the secret path relative to the mount root (no leading\n" +
			"slash, no mount prefix). The result envelope carries\n" +
			"{data: {...}, version: <int>}.\n\n" +
			"This replaces the consumer's `_secret-read.sh secret/<mount>/<path>`\n" +
			"wrapper. The dispatch path is the same /api/v1/operations/call route\n" +
			"the agent surface uses — auth, audit, broadcast, and policy gates\n" +
			"all run as documented in CLAUDE.md §6.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vault kv read --target rdc-vault secret meho/test/federation\n" +
			"  meho vault kv read --target rdc-vault secret app/db --json | jq .result.data",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindKVAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return dispatchKV(cmd, f, opKVRead, kvPathParams(args[0], args[1]))
	}
	return cmd
}

// newKVListCmd returns `meho vault kv list <mount> <path>`.
func newKVListCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "list <mount> <path>",
		Short: "List keys at a KV-v2 path",
		Long: "list dispatches op_id=\"vault.kv.list\" against connector_id=\n" +
			"\"vault-1.x\". Lists the child keys at <mount>/<path> via the\n" +
			"KV-v2 metadata endpoint. The result is set-shaped: when the key\n" +
			"count crosses the backplane's JSONFlux threshold the envelope\n" +
			"carries a result handle + sample instead of the full list — drill\n" +
			"in with `meho operation` result verbs (result_query / result_export)\n" +
			"exactly as for any other connector's set-shaped op.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vault kv list --target rdc-vault secret meho\n" +
			"  meho vault kv list --target rdc-vault secret meho/test --json",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindKVAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return dispatchKV(cmd, f, opKVList, kvPathParams(args[0], args[1]))
	}
	return cmd
}

// newKVPutCmd returns `meho vault kv put <mount> <path> --data ...`.
func newKVPutCmd() *cobra.Command {
	var (
		dataFlag string
		casFlag  int
		casSet   bool
	)
	cmd := &cobra.Command{
		Use:   "put <mount> <path>",
		Short: "Write a new version of a KV-v2 secret",
		Long: "put dispatches op_id=\"vault.kv.put\" against connector_id=\n" +
			"\"vault-1.x\". --data accepts the secret body as inline JSON or\n" +
			"@<file> (a JSON object of key/value pairs). --cas N enables a\n" +
			"check-and-set write: the put only succeeds if the secret's current\n" +
			"version equals N (use --cas 0 to assert the secret does not yet\n" +
			"exist). This is a write op — the dispatcher emits an audit row at\n" +
			"op_class=write.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vault kv put --target rdc-vault secret app/db --data '{\"password\":\"s3cr3t\"}'\n" +
			"  meho vault kv put --target rdc-vault secret app/db --data @secret.json --cas 3",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindKVAddrFlags(cmd)
	cmd.Flags().StringVar(&dataFlag, "data", "",
		"secret body as inline JSON object or @<file>; required")
	cmd.Flags().IntVar(&casFlag, "cas", 0,
		"check-and-set: only write if the current version equals this value")
	_ = cmd.MarkFlagRequired("data")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		// cobra populates casFlag even when the operator didn't pass
		// --cas; Changed() distinguishes "explicitly --cas 0" (a real
		// "must-not-exist" assertion) from "flag absent".
		casSet = cmd.Flags().Changed("cas")
		secret, err := loadJSONFlag(dataFlag)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), f.jsonOut)
		}
		params := kvPathParams(args[0], args[1])
		params["data"] = secret
		if casSet {
			params["cas"] = casFlag
		}
		return dispatchKV(cmd, f, opKVPut, params)
	}
	return cmd
}

// newKVVersionsCmd returns `meho vault kv versions <mount> <path>`.
func newKVVersionsCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "versions <mount> <path>",
		Short: "List the version history of a KV-v2 secret",
		Long: "versions dispatches op_id=\"vault.kv.versions\" against\n" +
			"connector_id=\"vault-1.x\". Read-only browse of the secret's\n" +
			"version map (version number → created/deleted/destroyed\n" +
			"timestamps) from the KV-v2 metadata endpoint.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vault kv versions --target rdc-vault secret app/db\n" +
			"  meho vault kv versions --target rdc-vault secret app/db --json",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindKVAddrFlags(cmd)
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		return dispatchKV(cmd, f, opKVVersions, kvPathParams(args[0], args[1]))
	}
	return cmd
}

// newKVDeleteCmd returns `meho vault kv delete <mount> <path> --versions`.
func newKVDeleteCmd() *cobra.Command {
	var versionsFlag string
	cmd := &cobra.Command{
		Use:   "delete <mount> <path>",
		Short: "Soft-delete specific versions of a KV-v2 secret",
		Long: "delete dispatches op_id=\"vault.kv.delete\" against connector_id=\n" +
			"\"vault-1.x\". --versions is a comma-separated list of version\n" +
			"numbers to soft-delete (Vault KV-v2 reversible delete — the\n" +
			"versions can be undeleted). This is a write op — the dispatcher\n" +
			"emits an audit row at op_class=write.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vault kv delete --target rdc-vault secret app/db --versions 3\n" +
			"  meho vault kv delete --target rdc-vault secret app/db --versions 3,4,5",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	f := bindKVAddrFlags(cmd)
	cmd.Flags().StringVar(&versionsFlag, "versions", "",
		"comma-separated version numbers to soft-delete (e.g. 3,4,5); required")
	_ = cmd.MarkFlagRequired("versions")
	cmd.RunE = func(cmd *cobra.Command, args []string) error {
		versions, err := parseVersionList(versionsFlag)
		if err != nil {
			return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), f.jsonOut)
		}
		params := kvPathParams(args[0], args[1])
		params["versions"] = versions
		return dispatchKV(cmd, f, opKVDelete, params)
	}
	return cmd
}

// parseVersionList turns "3,4,5" into []int{3,4,5}. The G3.3-T1
// vault.kv.delete handler's schema requires `versions` to be a
// non-empty array of integers; parsing client-side gives the operator
// an argv-level error ("--versions \"x\": …") instead of a backend
// schema-validation rejection round-trip. Whitespace around each
// element is tolerated (`3, 4`); an empty / non-integer element is an
// error.
func parseVersionList(s string) ([]int, error) {
	parts := strings.Split(s, ",")
	out := make([]int, 0, len(parts))
	for _, p := range parts {
		t := strings.TrimSpace(p)
		if t == "" {
			return nil, fmt.Errorf("--versions %q: empty version element", s)
		}
		n, err := strconv.Atoi(t)
		if err != nil {
			return nil, fmt.Errorf("--versions %q: %q is not an integer", s, t)
		}
		out = append(out, n)
	}
	return out, nil
}
