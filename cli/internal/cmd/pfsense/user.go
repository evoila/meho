// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package pfsense

import (
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newUserCmd returns the `meho pfsense user` parent with one sub-verb:
// `list` (pfsense.user.list).
func newUserCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "user",
		Short:        "pfSense local-user sub-verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newUserListCmd())
	return cmd
}

// newUserListCmd returns the `meho pfsense user list` command.
//
// Maps to op_id `pfsense.user.list`. Reads `config.xml` over SSH and
// returns one row per local user with a short list of fields (name,
// descr, scope, disabled, expires, uid, groups). Secret fields are never
// read; a value with a known secret shape comes back as ***REDACTED***.
func newUserListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List pfSense local users (secret fields are never read)",
		Long: "list dispatches pfsense.user.list and shows the local users\n" +
			"(name / disabled / expires / groups / full name). The op never\n" +
			"reads secret fields such as password hashes, keys or certificate\n" +
			"data. A value with a known secret shape comes back as\n" +
			"***REDACTED***. A secret typed in plain words into the full name\n" +
			"can still show: the same limit as `config show`.\n" +
			"Use it instead of `config show` to check a user, for example a\n" +
			"VPN user.\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"Exit codes: 0=ok, 1=error/denied, 2=auth_expired,\n" +
			"3=unreachable, 4=unexpected.",
		Example: "  meho pfsense user list --target fw-01\n" +
			"  meho pfsense user list --target fw-01 --json | jq '.result.rows[]'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runUserList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "",
		"target slug to dispatch against (required)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL from the most recent `meho login`)")
	return cmd
}

func runUserList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "pfsense.user.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "pfsense.user.list", r, jsonOut, printUserList)
}

func printUserList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s pfsense.user.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil || rows == nil {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  %-20s %-8s %-11s %-24s %s\n",
		"NAME", "DISABLED", "EXPIRES", "GROUPS", "DESCR")
	for _, row := range rows {
		name := stringField(row, "name")
		disabled := "no"
		if d, ok := row["disabled"].(bool); ok && d {
			disabled = "YES"
		}
		expires := stringField(row, "expires")
		if expires == "" {
			expires = "-"
		}
		var groups string
		if list, ok := row["groups"].([]any); ok {
			parts := make([]string, 0, len(list))
			for _, g := range list {
				if s, ok := g.(string); ok {
					parts = append(parts, s)
				}
			}
			groups = strings.Join(parts, ",")
		}
		descr := truncate(stringField(row, "descr"), 30)
		fmt.Fprintf(w, "  %-20s %-8s %-11s %-24s %s\n",
			name, disabled, expires, truncate(groups, 24), descr)
	}
	fmt.Fprintf(w, "  (%d users)\n", len(rows))
}
