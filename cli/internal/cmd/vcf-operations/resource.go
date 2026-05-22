// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vcfoperations

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/backplane"
	"github.com/evoila/meho/cli/internal/output"
)

// newResourceCmd returns the `meho vcf-operations resource` parent
// command (list + get sub-verbs).
func newResourceCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "resource",
		Short:        "vROps resource verbs (list, get)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newResourceListCmd())
	cmd.AddCommand(newResourceGetCmd())
	return cmd
}

// newResourceListCmd returns `meho vcf-operations resource list` →
// GET:/suite-api/api/resources.
//
// --params is the escape hatch for filter query parameters
// (“resourceKind“ / “adapterKind“ / “name“ / “page“ /
// “pageSize“). Passing them as raw JSON keeps the verb thin and
// avoids the trap of declaring every vROps query string as a
// dedicated flag — the resource list takes >10 documented filters.
func newResourceListCmd() *cobra.Command {
	var (
		targetName        string
		paramsFlag        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List vROps resources (VMs, hosts, datastores, adapter instances)",
		Long: "list dispatches GET:/suite-api/api/resources against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders identifier /\n" +
			"resourceKey.name / resourceKey.resourceKindKey for human eyes;\n" +
			"--json emits the full envelope.\n\n" +
			"Filter via --params with one of the documented query parameters\n" +
			"(resourceKind / adapterKind / name / page / pageSize); see the\n" +
			"vROps suite-api docs for the full list.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations resource list --target rdc-vrops\n" +
			"  meho vcf-operations resource list --target rdc-vrops " +
			"--params '{\"resourceKind\":\"VirtualMachine\",\"pageSize\":50}'\n" +
			"  meho vcf-operations resource list --target rdc-vrops --json",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runResourceList(cmd, targetName, paramsFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().StringVar(&paramsFlag, "params", "", "filter params as inline JSON or @<file>")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runResourceList(cmd *cobra.Command, targetName, paramsFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	params, err := loadParamsFlag(paramsFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	const opID = "GET:/suite-api/api/resources"
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printResourceList)
}

func printResourceList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/resources — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeVropsListResult(r.Result, vropsListKeysByOp["GET:/suite-api/api/resources"])
	if err != nil {
		fallbackResultRender(w, r)
		return
	}
	if len(entries) == 0 {
		fmt.Fprintln(w, "  (0 resources)")
		return
	}
	fmt.Fprintf(w, "%-38s %-30s %-22s\n", "identifier", "name", "kind")
	for _, e := range entries {
		fmt.Fprintf(w, "%-38s %-30s %-22s\n",
			truncate(vropsStringField(e, "identifier"), 38),
			truncate(vropsResourceName(e), 30),
			truncate(vropsResourceKindKey(e), 22),
		)
	}
}

// newResourceGetCmd returns `meho vcf-operations resource get <id>` →
// GET:/suite-api/api/resources/{id}.
//
// The descriptor declares “{id}“ as a path parameter; the CLI
// passes it under params so the dispatcher's “_substitute_path“
// fills it in at dispatch time (same pattern Harbor uses for
// “{project_name}“).
func newResourceGetCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "get <id>",
		Short: "Get one vROps resource by identifier (UUID)",
		Long: "get dispatches GET:/suite-api/api/resources/{id} against\n" +
			"connector_id=\"vrops-rest-9.0\". Renders the identifier,\n" +
			"resourceKey.name, resourceKey.resourceKindKey, and the\n" +
			"resourceStatusStates summary; --json emits the full envelope.\n\n" +
			"<id> is the resource UUID returned by `resource list`.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vcf-operations resource get 00000000-0000-4000-8000-000000000000 --target rdc-vrops\n" +
			"  meho vcf-operations resource get <uuid> --target rdc-vrops --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runResourceGet(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "vROps target slug")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runResourceGet(cmd *cobra.Command, id, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := backplane.Resolve(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), backplane.ClassifyError(err), jsonOut)
	}
	const opID = "GET:/suite-api/api/resources/{id}"
	params := map[string]any{"id": id}
	r, err := conn.Call(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return conn.Render(cmd, opID, r, jsonOut, printResourceGet)
}

func printResourceGet(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/suite-api/api/resources/{id} — status=%s (%.0fms)\n",
		ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	var entry map[string]any
	if err := jsonUnmarshalStrict(r.Result, &entry); err != nil {
		fallbackResultRender(w, r)
		return
	}
	if id := vropsStringField(entry, "identifier"); id != "" {
		fmt.Fprintf(w, "  identifier:  %s\n", id)
	}
	if name := vropsResourceName(entry); name != "" {
		fmt.Fprintf(w, "  name:        %s\n", name)
	}
	if kind := vropsResourceKindKey(entry); kind != "" {
		fmt.Fprintf(w, "  kind:        %s\n", kind)
	}
	if states, ok := entry["resourceStatusStates"].([]any); ok && len(states) > 0 {
		if first, ok := states[0].(map[string]any); ok {
			if rs, ok := first["resourceStatus"].(string); ok && rs != "" {
				fmt.Fprintf(w, "  status:      %s\n", rs)
			}
			if rstate, ok := first["resourceState"].(string); ok && rstate != "" {
				fmt.Fprintf(w, "  state:       %s\n", rstate)
			}
		}
	}
}
