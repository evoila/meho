// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"text/tabwriter"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/output"
)

// classifyNote is the help text shared by every command that changes a
// classification.
const classifyNote = "A category decides who may approve, so a change of category,\n" +
	"labels or \"always wait\" is a change of rights: it waits until\n" +
	"another tenant admin approves it (`meho approvals list`). It needs\n" +
	"the POLICY_CLASSIFICATION setting on the backplane.\n"

// newClassifyCmd returns the `meho connector classify` command.
//
// CLI shape:
//
//	meho connector classify [--connector <pattern>] [--group <pattern>] [--op <pattern>] \
//	  [--category <name>] [--label key=value]... [--always-wait | --no-always-wait] \
//	  [--apply] [--json] [--backplane <url>]
//
// Without --apply it is the dry run (POST /api/v1/policy/classification/preview):
// every matched operation, before and after; nothing is written. With
// --apply it asks for the change (POST /api/v1/policy/classification).
func newClassifyCmd() *cobra.Command {
	var (
		connectorFlag     string
		groupFlag         string
		opFlag            string
		category          string
		labels            []string
		alwaysWait        bool
		noAlwaysWait      bool
		apply             bool
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "classify",
		Short: "Set the category, labels or \"always wait\" of operations, one at a time or by pattern",
		Long: "classify gives one operation, one group, one connector, or many\n" +
			"operations by pattern a category, labels or the \"always wait\" mark.\n" +
			"In a pattern, `*` is any text and `?` is one character; without a\n" +
			"wildcard the value must match exactly. Every flag you give must match.\n\n" +
			"Without --apply the command is a dry run: it lists every operation\n" +
			"the match finds, with its values before and after, and writes nothing.\n" +
			"Check the list, then run the same command with --apply.\n\n" +
			classifyNote + "\n" +
			"Examples:\n" +
			"  meho connector classify --op 'k8s.deployment.*' --category compute\n" +
			"  meho connector classify --connector <connector-id> --label team=<team-name> --apply\n\n" +
			"Role: tenant_admin.",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			body, err := classificationBody(connectorFlag, groupFlag, opFlag, category, labels,
				alwaysWaitValue(alwaysWait, noAlwaysWait))
			if err != nil {
				return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
			}
			return runClassify(cmd, body, apply, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&connectorFlag, "connector", "", "connector id pattern, for example vault-1.x")
	cmd.Flags().StringVar(&groupFlag, "group", "", "operation group key pattern")
	cmd.Flags().StringVar(&opFlag, "op", "", "operation id pattern, for example 'k8s.deployment.*'")
	addClassificationFlags(cmd, &category, &labels)
	cmd.Flags().BoolVar(&alwaysWait, "always-wait", false, "every call of a matched operation waits for approval")
	cmd.Flags().BoolVar(&noAlwaysWait, "no-always-wait", false,
		"back to the operation's own approval value (never below it)")
	cmd.Flags().BoolVar(&apply, "apply", false, "ask for the change (without it: dry run only)")
	cmd.Flags().BoolVar(&jsonOut, "json", false,
		"emit machine-readable JSON to stdout instead of the human summary")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	cmd.MarkFlagsMutuallyExclusive("always-wait", "no-always-wait")
	return cmd
}

// addClassificationFlags adds --category and --label to a command.
func addClassificationFlags(cmd *cobra.Command, category *string, labels *[]string) {
	cmd.Flags().StringVar(category, "category", "", "a category of the taxonomy, for example network")
	cmd.Flags().StringArrayVar(labels, "label", nil,
		"a key=value label (repeat for more); replaces the labels of this entry")
}

func alwaysWaitValue(on, off bool) *bool {
	switch {
	case on:
		t := true
		return &t
	case off:
		f := false
		return &f
	}
	return nil
}

// parseLabels turns repeated key=value flags into a map; nil for none.
func parseLabels(values []string) (*map[string]string, error) {
	if len(values) == 0 {
		return nil, nil
	}
	labels := make(map[string]string, len(values))
	for _, value := range values {
		key, val, ok := strings.Cut(value, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" || strings.TrimSpace(val) == "" {
			return nil, fmt.Errorf("--label %q: write it as key=value", value)
		}
		labels[key] = strings.TrimSpace(val)
	}
	return &labels, nil
}

func optional(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

func classificationBody(
	connectorID, group, op, category string, labels []string, alwaysWait *bool,
) (api.ClassificationEditBody, error) {
	body := api.ClassificationEditBody{
		Match: api.ClassificationMatch{
			Connector: optional(connectorID),
			Group:     optional(group),
			Op:        optional(op),
		},
		Category:   optional(category),
		AlwaysWait: alwaysWait,
	}
	if body.Match.Connector == nil && body.Match.Group == nil && body.Match.Op == nil {
		return body, errors.New("classify needs at least one of --connector, --group or --op")
	}
	parsed, err := parseLabels(labels)
	if err != nil {
		return body, err
	}
	body.Labels = parsed
	if body.Category == nil && body.Labels == nil && body.AlwaysWait == nil {
		return body, errors.New("classify needs at least one of --category, --label, --always-wait or --no-always-wait")
	}
	return body, nil
}

func runClassify(cmd *cobra.Command, body api.ClassificationEditBody, apply, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	ctx := cmd.Context()
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	if !apply {
		resp, err := retryOn401(ctx, authed,
			func(ctx context.Context) (*api.PreviewClassificationApiV1PolicyClassificationPreviewPostResponse, error) {
				return authed.PreviewClassificationApiV1PolicyClassificationPreviewPostWithResponse(
					ctx, &api.PreviewClassificationApiV1PolicyClassificationPreviewPostParams{}, body)
			},
			func(r *api.PreviewClassificationApiV1PolicyClassificationPreviewPostResponse) int {
				return r.StatusCode()
			},
		)
		if err != nil {
			return renderRequestError(cmd, backplaneURL, err, jsonOut)
		}
		if resp.StatusCode() != http.StatusOK || resp.JSON200 == nil {
			return renderHTTPStatus(cmd, backplaneURL, resp.StatusCode(), resp.Body, jsonOut)
		}
		if jsonOut {
			return output.PrintJSON(cmd.OutOrStdout(), resp.JSON200)
		}
		printClassificationPreview(cmd.OutOrStdout(), resp.JSON200)
		return nil
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.RequestClassificationApiV1PolicyClassificationPostResponse, error) {
			return authed.RequestClassificationApiV1PolicyClassificationPostWithResponse(
				ctx, &api.RequestClassificationApiV1PolicyClassificationPostParams{}, body)
		},
		func(r *api.RequestClassificationApiV1PolicyClassificationPostResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderClassificationRequest(cmd, backplaneURL, resp.StatusCode(), resp.Body, resp.JSON202, jsonOut)
}

// renderClassificationRequest prints the answer to a classification
// request: the id of the approval request it waits for.
func renderClassificationRequest(
	cmd *cobra.Command, backplaneURL string, status int, raw []byte,
	resp *api.ClassificationRequestResponse, jsonOut bool,
) error {
	if status != http.StatusAccepted || resp == nil {
		return renderHTTPStatus(cmd, backplaneURL, status, raw, jsonOut)
	}
	if jsonOut {
		return output.PrintJSON(cmd.OutOrStdout(), resp)
	}
	w := cmd.OutOrStdout()
	fmt.Fprintf(w, "%s (%d operation(s) matched, %d change)\n",
		resp.Message, resp.MatchedOperations, resp.ChangedOperations)
	if resp.ApprovalRequestId != nil {
		fmt.Fprintf(w, "  approval request: %s\n", *resp.ApprovalRequestId)
	}
	return nil
}

func printClassificationPreview(w io.Writer, preview *api.ClassificationPreviewResponse) {
	fmt.Fprintf(w, "Dry run: %d operation(s) matched, %d change. Nothing is stored.\n",
		preview.MatchedOperations, preview.ChangedOperations)
	if preview.Conflict != nil {
		fmt.Fprintf(w, "conflict: %s\n", *preview.Conflict)
	}
	if len(preview.Operations) == 0 {
		return
	}
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "CONNECTOR\tGROUP\tOPERATION\tCATEGORY\tLABELS\tALWAYS WAIT\tIN FORCE")
	for _, row := range preview.Operations {
		group := ""
		if row.GroupKey != nil {
			group = *row.GroupKey
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s -> %s\t%s -> %s\t%t -> %t\t%s -> %s\n",
			row.ConnectorId, group, row.OpId,
			valueOr(row.Before.Category, "-"), valueOr(row.After.Category, "-"),
			labelText(row.Before.Labels), labelText(row.After.Labels),
			row.Before.AlwaysWait, row.After.AlwaysWait,
			inForceText(row.Before.InForce), inForceText(row.After.InForce),
		)
	}
	_ = tw.Flush()
	if preview.Conflict == nil && preview.MatchedOperations > 0 {
		fmt.Fprintln(w, "Run the same command with --apply to ask for the change.")
	}
}

// inForceText is what applies with the values: the code's value, the
// overrides and the minimum-level rules included. "-" when the operation is gone.
func inForceText(inForce api.ClassificationInForce) string {
	if inForce.Risk == "" {
		return "-"
	}
	if inForce.Waits {
		return inForce.Risk + ", waits"
	}
	return inForce.Risk + ", runs at once"
}

func valueOr(value *string, fallback string) string {
	if value == nil {
		return fallback
	}
	return *value
}

func labelText(labels map[string]string) string {
	if len(labels) == 0 {
		return "-"
	}
	keys := make([]string, 0, len(labels))
	for key := range labels {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, key+"="+labels[key])
	}
	return strings.Join(parts, ",")
}

// classificationFlagsSet reports whether --category or --label was given,
// and refuses them next to any of the other edit flags: a classification
// change waits for another admin, the other edits are stored at once.
func classificationFlagsSet(cmd *cobra.Command, otherFlags ...string) (bool, error) {
	if !cmd.Flags().Changed("category") && !cmd.Flags().Changed("label") {
		return false, nil
	}
	for _, name := range otherFlags {
		if cmd.Flags().Changed(name) {
			return true, fmt.Errorf(
				"--category and --label wait for another tenant admin; use them without --%s", name)
		}
	}
	return true, nil
}

// patchOpClassification asks for a category / labels change of one operation.
func patchOpClassification(
	cmd *cobra.Command, backplaneURL, connectorID, opID string, category string, labels []string, jsonOut bool,
) error {
	parsed, err := parseLabels(labels)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	body := api.EditOpBody{Category: optional(category), Labels: parsed}
	ctx := cmd.Context()
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchResponse, error) {
			return authed.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchWithResponse(
				ctx, connectorID, opID, &api.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchParams{}, body)
		},
		func(r *api.EditOpEndpointApiV1ConnectorsConnectorIdOperationsOpIdPatchResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderClassificationRequest(cmd, backplaneURL, resp.StatusCode(), resp.Body, resp.JSON202, jsonOut)
}

// patchGroupClassification asks for a category / labels change of one group.
func patchGroupClassification(
	cmd *cobra.Command, backplaneURL, connectorID, groupKey string, category string, labels []string, jsonOut bool,
) error {
	parsed, err := parseLabels(labels)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	body := api.EditGroupBody{Category: optional(category), Labels: parsed}
	ctx := cmd.Context()
	authed, err := newAuthedClient(ctx, backplaneURL)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	resp, err := retryOn401(ctx, authed,
		func(ctx context.Context) (*api.EditGroupEndpointApiV1ConnectorsConnectorIdGroupsGroupKeyPatchResponse, error) {
			return authed.EditGroupEndpointApiV1ConnectorsConnectorIdGroupsGroupKeyPatchWithResponse(
				ctx, connectorID, groupKey, &api.EditGroupEndpointApiV1ConnectorsConnectorIdGroupsGroupKeyPatchParams{}, body)
		},
		func(r *api.EditGroupEndpointApiV1ConnectorsConnectorIdGroupsGroupKeyPatchResponse) int {
			return r.StatusCode()
		},
	)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderClassificationRequest(cmd, backplaneURL, resp.StatusCode(), resp.Body, resp.JSON202, jsonOut)
}
