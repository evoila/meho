// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package gcloud

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newProjectCmd returns the `meho gcloud project` parent command and
// assembles its describe verb.
func newProjectCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "project",
		Short:        "GCP project verbs (describe)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newProjectDescribeCmd())
	return cmd
}

// newProjectDescribeCmd returns `meho gcloud project describe`. Maps
// to op_id `gcloud.project.describe`. Output is the raw CRM v1 project
// resource dict (projectId, projectNumber, name, lifecycleState,
// createTime, labels, parent).
func newProjectDescribeCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "describe",
		Short: "Return the full Cloud Resource Manager project resource",
		Long: "describe dispatches gcloud.project.describe against\n" +
			"connector_id=\"gcloud-rest-1.0\". Returns the raw CRM v1 project\n" +
			"resource dict including all fields (projectId, projectNumber,\n" +
			"name, lifecycleState, createTime, labels, parent). Use when\n" +
			"the full structured resource is needed (e.g. to read custom\n" +
			"labels or the exact parent type).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho gcloud project describe --target rdc-gcp-dev\n" +
			"  meho gcloud project describe --target rdc-gcp-dev --json | jq .result.labels",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runProjectDescribe(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runProjectDescribe(
	cmd *cobra.Command,
	targetName string,
	jsonOut bool,
	backplaneOverride string,
) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "gcloud.project.describe", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "gcloud.project.describe", r, jsonOut, printProjectDescribe)
}

// printProjectDescribe renders the project describe result. Surfaces
// the key fields operators typically need (projectId, lifecycleState,
// createTime, parent, labels) in a compact format; --json emits the
// full envelope for deeper inspection.
func printProjectDescribe(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s gcloud.project.describe — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	proj, err := decodeFlatResult(r.Result)
	if err != nil || proj == nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"projectId", "projectNumber", "name", "lifecycleState", "createTime"} {
		v, ok := proj[key]
		if !ok || v == nil {
			continue
		}
		fmt.Fprintf(w, "  %-20s %v\n", key+":", v)
	}
	if parent, ok := proj["parent"].(map[string]any); ok && parent != nil {
		fmt.Fprintf(w, "  %-20s %v/%v\n", "parent:", parent["type"], parent["id"])
	}
	if labels, ok := proj["labels"].(map[string]any); ok && len(labels) > 0 {
		fmt.Fprintf(w, "  labels:\n")
		for k, v := range labels {
			fmt.Fprintf(w, "    %s = %v\n", k, v)
		}
	}
}
