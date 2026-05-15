// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newVMCmd returns the `meho vmware vm` parent command and assembles
// its three verbs (list / info / create). The parent itself takes no
// args and prints its own help.
func newVMCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "vm",
		Short:        "vSphere VM verbs (list / info / create)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newVMListCmd())
	cmd.AddCommand(newVMInfoCmd())
	cmd.AddCommand(newVMCreateCmd())
	return cmd
}

// newVMListCmd returns `meho vmware vm list`.
//
// Maps to op_id `GET:/vcenter/vm`. Optional --filter flags expose
// the vSphere REST filter parameters (powered_states, names,
// clusters, hosts, etc.); the v0.2 surface ships only --names and
// --power-state because those are the two filters every operator
// reaches for first. Additional filters land via --filter "k=v"
// repeats which marshal into the params map verbatim.
func newVMListCmd() *cobra.Command {
	var (
		targetName        string
		filterNames       []string
		filterPowerStates []string
		filterRaw         []string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List VMs on a vCenter target",
		Long: "list dispatches GET:/vcenter/vm against the connector_id=\n" +
			"\"vmware-rest-9.0\" connector. Optional filters narrow the result;\n" +
			"the human render shows name / power_state / cpu / memory_size,\n" +
			"--json emits the full OperationResult envelope.\n\n" +
			"--names accepts repeated values to filter by VM name; --power-state\n" +
			"narrows by powered_states (POWERED_ON / POWERED_OFF / SUSPENDED).\n" +
			"--filter \"k=v\" is the escape hatch for filters the dedicated flags\n" +
			"don't cover (clusters, hosts, datacenters, folders, etc.).\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware vm list --target rdc-vcenter\n" +
			"  meho vmware vm list --target rdc-vcenter --power-state POWERED_ON\n" +
			"  meho vmware vm list --target rdc-vcenter --json | jq '.result[].name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runVMList(cmd, vmListOpts{
				TargetName:        targetName,
				FilterNames:       filterNames,
				FilterPowerStates: filterPowerStates,
				FilterRaw:         filterRaw,
				JSONOut:           jsonOut,
				BackplaneOverride: backplaneOverride,
			})
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringArrayVar(&filterNames, "names", nil,
		"filter by VM name; repeat for multiple matches")
	cmd.Flags().StringArrayVar(&filterPowerStates, "power-state", nil,
		"filter by powered_states (POWERED_ON / POWERED_OFF / SUSPENDED); repeat for OR")
	cmd.Flags().StringArrayVar(&filterRaw, "filter", nil,
		"raw vSphere filter as k=v; repeat for multiple filters (e.g. --filter clusters=domain-c1)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

type vmListOpts struct {
	TargetName        string
	FilterNames       []string
	FilterPowerStates []string
	FilterRaw         []string
	JSONOut           bool
	BackplaneOverride string
}

func runVMList(cmd *cobra.Command, opts vmListOpts) error {
	backplaneURL, err := resolveBackplane(opts.BackplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), opts.JSONOut)
	}
	params, perr := buildListParams(opts.FilterNames, opts.FilterPowerStates, opts.FilterRaw)
	if perr != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(perr.Error()), opts.JSONOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "GET:/vcenter/vm", opts.TargetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, opts.JSONOut)
	}
	return renderCallResult(cmd, "GET:/vcenter/vm", r, opts.JSONOut, printVMList)
}

// printVMList renders a VM list as a table. vSphere's GET /vcenter/vm
// returns entries with `vm` (moid), `name`, `power_state`, `cpu_count`,
// `memory_size_MiB`. The renderer falls back to the generic JSON
// dump when the shape doesn't decode (per-endpoint drift surfaces
// in the fallback rather than as a panic).
func printVMList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/vcenter/vm — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	entries, err := decodeListResult(r.Result)
	if err != nil || len(entries) == 0 {
		if err == nil {
			fmt.Fprintln(w, "  (0 VMs)")
			return
		}
		// Fall back to raw render on decode failure.
		fallbackResultRender(w, r)
		return
	}
	fmt.Fprintf(w, "%-16s %-30s %-12s %4s %10s\n", "moid", "name", "power", "cpu", "memMiB")
	for _, e := range entries {
		moid := stringField(e, "vm")
		name := stringField(e, "name")
		power := stringField(e, "power_state")
		cpu := intField(e, "cpu_count")
		mem := intField(e, "memory_size_MiB")
		fmt.Fprintf(w, "%-16s %-30s %-12s %4d %10d\n",
			truncate(moid, 16),
			truncate(name, 30),
			truncate(power, 12),
			cpu,
			mem,
		)
	}
}

// fallbackResultRender dumps the result envelope verbatim when the
// typed per-verb decode fails. Used by every verb's pretty-printer
// so contract drift surfaces with the same affordance.
func fallbackResultRender(w io.Writer, r *CallResult) {
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	pretty, err := prettyJSON(r.Result)
	if err == nil {
		fmt.Fprintln(w, pretty)
		return
	}
	fmt.Fprintln(w, string(r.Result))
}

// stringField pulls a string field from a vSphere list entry,
// returning empty string when the field is missing or wrong type.
// The vSphere REST API stays disciplined about field shapes but the
// dispatcher pass-through preserves whatever the backend emitted,
// including JSON null on optional fields — empty is the safe render.
func stringField(e listEntry, key string) string {
	v, ok := e[key]
	if !ok {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// intField pulls an integer field from a vSphere list entry. JSON
// integers land as float64 after json.Unmarshal into map[string]any
// — the conversion is documented behaviour, not a workaround.
func intField(e listEntry, key string) int {
	v, ok := e[key]
	if !ok {
		return 0
	}
	if f, ok := v.(float64); ok {
		return int(f)
	}
	return 0
}

// buildListParams folds the typed filter flags + raw --filter k=v
// repeats into the params map the dispatcher passes through. The
// vSphere REST filter syntax uses dotted keys (`filter.names`,
// `filter.power_states`) — the helper writes them verbatim so the
// shape lines up with the endpoint_descriptor's documented inputs.
//
// --filter raw values follow `k=v` with a single `=` separator.
// Multi-equals values (`k=a=b`) split on the first `=` so the value
// can itself contain `=`. Missing `=` returns an error.
func buildListParams(names, powerStates, raw []string) (map[string]any, error) {
	out := map[string]any{}
	if len(names) > 0 {
		out["filter.names"] = names
	}
	if len(powerStates) > 0 {
		out["filter.power_states"] = powerStates
	}
	for _, kv := range raw {
		k, v, ok := splitOnce(kv, "=")
		if !ok {
			return nil, fmt.Errorf("--filter %q: expected k=v form", kv)
		}
		out[k] = v
	}
	if len(out) == 0 {
		return nil, nil
	}
	return out, nil
}

// splitOnce splits s on the first occurrence of sep. Returns
// (before, after, true) when sep is found, (s, "", false) otherwise.
// strings.SplitN(s, sep, 2) handles the same case but with three
// allocations; this single-allocation form is in line with the
// project's allocation discipline on hot paths.
func splitOnce(s, sep string) (before, after string, ok bool) {
	for i := 0; i+len(sep) <= len(s); i++ {
		if s[i:i+len(sep)] == sep {
			return s[:i], s[i+len(sep):], true
		}
	}
	return s, "", false
}

// newVMInfoCmd returns `meho vmware vm info <name-or-id>`.
//
// Resolves <name-or-id> via resolveName(kind="vm") then dispatches
// GET:/vcenter/vm/{vm} with the resolved moid passed as the {vm}
// path parameter. The dispatcher's endpoint_descriptor declares
// {vm} as a path param; the params map carries it under that key.
func newVMInfoCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "info <name-or-id>",
		Short: "Show details for one VM by name or moid",
		Long: "info accepts a VM name (resolved client-side to a moid via\n" +
			"GET:/vcenter/vm?filter.names=<name>) or a moid directly. After\n" +
			"resolution, dispatches GET:/vcenter/vm/{vm} with the resolved\n" +
			"moid as the {vm} path parameter.\n\n" +
			"A name that resolves to 0 or >1 candidates exits with status 1\n" +
			"and a message naming the candidates so the operator can re-invoke\n" +
			"with the moid directly.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware vm info web-prod-01 --target rdc-vcenter\n" +
			"  meho vmware vm info vm-101 --target rdc-vcenter --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runVMInfo(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runVMInfo(cmd *cobra.Command, nameOrID, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	moid, err := resolveName(cmd.Context(), backplaneURL, targetName, "vm", nameOrID)
	if err != nil {
		// Name-resolution failures (not-found, ambiguous, dispatcher
		// error during the resolve round-trip) map to exit 1 — same
		// shape as a dispatcher-reported status==error. The verb
		// surfaces the resolver's prose without re-decorating it
		// because resolveName already names the kind / target.
		return output.RenderError(cmd.ErrOrStderr(),
			&output.StructuredError{
				Code:   "resolve_failed",
				Detail: err.Error(),
				Exit:   1,
			}, jsonOut)
	}
	opID := "GET:/vcenter/vm/{vm}"
	params := map[string]any{"vm": moid}
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, opID, r, jsonOut, printVMInfo)
}

// printVMInfo renders a single-VM detail block. The result body's
// shape on 9.0 includes name / power_state / cpu / memory / disks /
// nics / boot — operators read a flat property summary. We pull the
// top-level scalar fields and leave nested sequences to --json.
func printVMInfo(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s GET:/vcenter/vm/{vm} — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	if len(r.Result) == 0 || string(r.Result) == "null" {
		return
	}
	// vm info can come back bare or value-wrapped just like the about
	// endpoint. Unwrap before reading scalar fields.
	body := unwrapValue(r.Result)
	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		fallbackResultRender(w, r)
		return
	}
	for _, key := range []string{"name", "power_state", "cpu_count", "memory_size_MiB", "guest_OS"} {
		v, ok := m[key]
		if !ok {
			continue
		}
		fmt.Fprintf(w, "  %-17s %v\n", key+":", v)
	}
}

// unwrapValue strips the legacy `{"value": ...}` envelope when
// present; otherwise returns raw verbatim. Used by every per-verb
// renderer that wants to drop into a typed shape without branching
// on response form.
func unwrapValue(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return raw
	}
	var wrapped struct {
		Value json.RawMessage `json:"value"`
	}
	if err := json.Unmarshal(raw, &wrapped); err == nil && len(wrapped.Value) > 0 {
		return wrapped.Value
	}
	return raw
}

// newVMCreateCmd returns `meho vmware vm create`. Dispatches the
// composite op_id `vmware.composite.vm.create` (ships in T6 #509).
// Pre-merge of T6, the dispatcher returns "operation not found"
// which surfaces in the standard error trailer.
func newVMCreateCmd() *cobra.Command {
	var (
		targetName        string
		specFlag          string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "create",
		Short: "Create a VM via the composite create flow",
		Long: "create dispatches op_id=\"vmware.composite.vm.create\" against the\n" +
			"connector_id=\"vmware-rest-9.0\" connector. The composite orchestrates\n" +
			"the multi-step vSphere REST create flow (placement spec → create →\n" +
			"power-on policy → tag application) that ships in G3.1-T6 (#509).\n\n" +
			"--spec accepts inline JSON or @<file>; the JSON shape is the\n" +
			"vSphere `Vm.CreateSpec` model the composite consumes (see the T6\n" +
			"task body for the field list).\n\n" +
			"Pre-merge of T6 (#509), the dispatcher returns \"operation not\n" +
			"found\" which surfaces in the standard error trailer.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho vmware vm create --target rdc-vcenter --spec @new-vm.json\n" +
			"  meho vmware vm create --target rdc-vcenter --spec '{\"name\":\"x\",\"power_on\":true}'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runVMCreate(cmd, targetName, specFlag, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().StringVar(&specFlag, "spec", "", "vSphere CreateSpec as inline JSON or @<file>; required")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	_ = cmd.MarkFlagRequired("spec")
	return cmd
}

func runVMCreate(cmd *cobra.Command, targetName, specFlag string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params, err := loadParamsFlag(specFlag)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), output.Unexpected(err.Error()), jsonOut)
	}
	opID := "vmware.composite.vm.create"
	r, err := dispatchOp(cmd.Context(), backplaneURL, opID, targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	// The composite's success shape is opaque to the CLI today
	// (T6 ships the canonical envelope; CLI consumers read --json).
	// Use the generic renderer until the shape stabilises.
	return renderCallResult(cmd, opID, r, jsonOut, nil)
}
