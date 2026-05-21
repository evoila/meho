// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newArtifactCmd returns `meho harbor artifact` with list / info subcommands.
func newArtifactCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "artifact",
		Short:        "List or inspect Harbor artifacts within a repository",
		SilenceUsage: true,
	}
	cmd.AddCommand(newArtifactListCmd())
	cmd.AddCommand(newArtifactInfoCmd())
	return cmd
}

func newArtifactListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list <project_name> <repository_name>",
		Short: "List artifacts (tags + digests) in a Harbor repository",
		Long: "list dispatches GET:.../artifacts against connector_id=\"harbor-rest-2.x\"\n" +
			"and renders a table of tags, digests, and push times.\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor artifact list library ubuntu --target prod-harbor\n" +
			"  meho harbor artifact list myproject myimage --target prod-harbor --json",
		Args:          cobra.ExactArgs(2),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runArtifactList(cmd, args[0], args[1], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runArtifactList(cmd *cobra.Command, projectName, repoName, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	opID := "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts"
	params := map[string]any{
		"project_name":    projectName,
		"repository_name": repoName,
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printArtifactList)
}

func printArtifactList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:.../artifacts — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	items, err := decodeHarborList(r.Result)
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(items) == 0 {
		fmt.Fprintf(w, "  (0 artifacts)\n")
		return
	}
	fmt.Fprintf(w, "%-20s %-72s  %s\n", "tag", "digest", "pushed")
	for _, artifact := range items {
		digest, _ := artifact["digest"].(string)
		pushTime, _ := artifact["push_time"].(string)
		tags, _ := artifact["tags"].([]any)
		if len(tags) == 0 {
			fmt.Fprintf(w, "%-20s %-72s  %s\n", "<untagged>", truncate(digest, 72), truncate(pushTime, 30))
			continue
		}
		for i, tagAny := range tags {
			tagMap, _ := tagAny.(map[string]any)
			tagName := ""
			if tagMap != nil {
				tagName, _ = tagMap["name"].(string)
			}
			if i == 0 {
				fmt.Fprintf(w, "%-20s %-72s  %s\n",
					truncate(tagName, 20), truncate(digest, 72), truncate(pushTime, 30))
			} else {
				fmt.Fprintf(w, "%-20s\n", truncate(tagName, 20))
			}
		}
	}
}

func newArtifactInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <project_name> <repository_name> <reference>",
		Short: "Show full metadata for a Harbor artifact by tag or digest",
		Long: "info dispatches GET:.../artifacts/{reference} against\n" +
			"connector_id=\"harbor-rest-2.x\" and renders full artifact metadata\n" +
			"including tags, digest, accessories (SBOM, signature), and scan overview.\n" +
			"<reference> is a tag name or digest (sha256:...).\n" +
			"--json emits the full OperationResult envelope.",
		Example: "  meho harbor artifact info library ubuntu latest --target prod-harbor\n" +
			"  meho harbor artifact info myproject myimage sha256:abc123 --target prod-harbor --json",
		Args:          cobra.ExactArgs(3),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runArtifactInfo(cmd, args[0], args[1], args[2], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "Harbor target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runArtifactInfo(cmd *cobra.Command, projectName, repoName, reference, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	opID := "GET:/api/v2.0/projects/{project_name}/repositories/{repository_name}/artifacts/{reference}"
	params := map[string]any{
		"project_name":    projectName,
		"repository_name": repoName,
		"reference":       reference,
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printArtifactInfo)
}

func printArtifactInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:.../artifacts/{ref} — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var artifact struct {
		Digest    string `json:"digest"`
		Size      int64  `json:"size"`
		PushTime  string `json:"push_time"`
		MediaType string `json:"media_type"`
		Tags      []struct {
			Name     string `json:"name"`
			PushTime string `json:"push_time"`
		} `json:"tags"`
		Accessories []struct {
			Type string `json:"type"`
		} `json:"accessories"`
	}
	if err := jsonUnmarshalStrict(r.Result, &artifact); err != nil || artifact.Digest == "" {
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "  digest:     %s\n", artifact.Digest)
	fmt.Fprintf(w, "  size:       %d bytes\n", artifact.Size)
	if artifact.PushTime != "" {
		fmt.Fprintf(w, "  pushed:     %s\n", artifact.PushTime)
	}
	if artifact.MediaType != "" {
		fmt.Fprintf(w, "  media_type: %s\n", artifact.MediaType)
	}
	if len(artifact.Tags) > 0 {
		for _, t := range artifact.Tags {
			fmt.Fprintf(w, "  tag:        %s\n", t.Name)
		}
	}
	for _, acc := range artifact.Accessories {
		fmt.Fprintf(w, "  accessory:  %s\n", acc.Type)
	}
}
