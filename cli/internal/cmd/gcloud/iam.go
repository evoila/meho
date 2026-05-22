// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newIamCmd returns the `meho gcloud iam` parent command and assembles
// its sub-tree:
//
//	gcloud iam sa list      — gcloud.iam.service_accounts.list
//	gcloud iam policy read  — gcloud.iam.policy.read
func newIamCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "iam",
		Short:        "GCP IAM verbs (service-account list, policy read)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIamSaCmd())
	cmd.AddCommand(newIamPolicyCmd())
	return cmd
}

// --- service accounts sub-group ---

// newIamSaCmd returns the `meho gcloud iam sa` parent command with its
// list verb.
func newIamSaCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "sa",
		Short:        "GCP service-account verbs (list)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIamSaListCmd())
	return cmd
}

// newIamSaListCmd returns `meho gcloud iam sa list`. Maps to op_id
// `gcloud.iam.service_accounts.list`. Output is the canonical
// `{rows, total}` envelope; rows carry `{email, unique_id,
// display_name, description, disabled}`.
func newIamSaListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List IAM service accounts in the project",
		Long: "list dispatches gcloud.iam.service_accounts.list against\n" +
			"connector_id=\"gcloud-rest-1.0\". Returns one row per service\n" +
			"account with email, unique_id, display_name, description, and\n" +
			"disabled status.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud iam sa list --target rdc-gcp-dev\n" +
			"  meho gcloud iam sa list --target rdc-gcp-dev --json | jq '.result.rows[].email'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runIamSaList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runIamSaList(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.iam.service_accounts.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.iam.service_accounts.list", r, jsonOut, printIamSaList)
}

// printIamSaList renders the service-account list. Each row carries
// `{email, unique_id, display_name, description, disabled}`.
func printIamSaList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.iam.service_accounts.list — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	rows, err := decodeRowsResult(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(rows) == 0 {
		fmt.Fprintln(w, "  (0 service accounts)")
		return
	}
	fmt.Fprintf(w, "%-55s %-10s %s\n", "email", "disabled", "display_name")
	for _, row := range rows {
		email := stringField(row, "email")
		disabled := boolField(row, "disabled")
		name := stringField(row, "display_name")
		disStr := "false"
		if disabled {
			disStr = "true"
		}
		fmt.Fprintf(w, "%-55s %-10s %s\n",
			truncate(email, 55),
			disStr,
			truncate(name, 40),
		)
	}
}

// --- IAM policy sub-group ---

// newIamPolicyCmd returns the `meho gcloud iam policy` parent command
// with its read verb.
func newIamPolicyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "policy",
		Short:        "GCP IAM policy verbs (read)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newIamPolicyReadCmd())
	return cmd
}

// newIamPolicyReadCmd returns `meho gcloud iam policy read`. Maps to
// op_id `gcloud.iam.policy.read`. Output is the full project IAM
// policy: version, etag, bindings.
func newIamPolicyReadCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "read",
		Short: "Read the project-level IAM policy (all role bindings)",
		Long: "read dispatches gcloud.iam.policy.read against\n" +
			"connector_id=\"gcloud-rest-1.0\". Returns the full project\n" +
			"IAM policy: version, etag, and all role→members bindings.\n" +
			"Use to audit who has which roles before investigating a\n" +
			"permission-denied failure or before assigning new roles.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud iam policy read --target rdc-gcp-dev\n" +
			"  meho gcloud iam policy read --target rdc-gcp-dev --json | jq '.result.bindings'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runIamPolicyRead(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runIamPolicyRead(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.iam.policy.read", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.iam.policy.read", r, jsonOut, printIamPolicyRead)
}

// printIamPolicyRead renders the IAM policy. Surfaces version, etag,
// and the role→members bindings table in the human path.
func printIamPolicyRead(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.iam.policy.read — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	policy, err := decodeFlatResult(r.Result)
	if err != nil || policy == nil {
		fallbackResultRender(w, r)
		return
	}
	if v, ok := policy["version"]; ok {
		fmt.Fprintf(w, "  version: %v\n", v)
	}
	if etag, ok := policy["etag"]; ok && etag != nil {
		fmt.Fprintf(w, "  etag:    %v\n", etag)
	}
	bindingsAny, _ := policy["bindings"].([]any)
	if len(bindingsAny) == 0 {
		fmt.Fprintln(w, "  (0 bindings)")
		return
	}
	fmt.Fprintf(w, "\n  %-50s members\n", "role")
	for _, ba := range bindingsAny {
		b, ok := ba.(map[string]any)
		if !ok {
			continue
		}
		role := stringField(b, "role")
		var members []string
		if ms, ok := b["members"].([]any); ok {
			for _, m := range ms {
				if s, ok := m.(string); ok {
					members = append(members, s)
				}
			}
		}
		// Print role with first member; subsequent members on indented lines.
		if len(members) == 0 {
			fmt.Fprintf(w, "  %-50s (0 members)\n", truncate(role, 50))
			continue
		}
		fmt.Fprintf(w, "  %-50s %s\n", truncate(role, 50), members[0])
		for _, m := range members[1:] {
			fmt.Fprintf(w, "  %-50s %s\n", "", m)
		}
		if len(members) > 5 {
			fmt.Fprintf(w, "  %-50s … (%d total)\n", "",
				len(members))
		}
	}
}
