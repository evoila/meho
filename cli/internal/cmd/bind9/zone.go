// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/output"
)

// newZoneCmd returns the `meho bind9 zone` parent command and
// assembles its two verbs (list / read). The parent itself takes no
// args and prints its own help.
func newZoneCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:          "zone",
		Short:        "bind9 zone verbs (list / read)",
		SilenceUsage: true,
	}
	cmd.AddCommand(newZoneListCmd())
	cmd.AddCommand(newZoneReadCmd())
	return cmd
}

// newZoneListCmd returns `meho bind9 zone list`. Maps to op_id
// `bind9.zone.list`. Output is the canonical `{rows: [...], total}`
// envelope; rows carry `{name, file, type}`.
func newZoneListCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "list",
		Short: "List zones declared in the active bind9 configuration",
		Long: "list dispatches bind9.zone.list against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector. Returns one row per zone declared\n" +
			"in the active configuration (parsed from `named-checkconf -p`).\n" +
			"The human render shows name / type / file columns; --json emits\n" +
			"the full OperationResult envelope.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 zone list --target vcf-router-bind9\n" +
			"  meho bind9 zone list --target vcf-router-bind9 --json | jq '.result.rows[].name'",
		Args:          cobra.NoArgs,
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runZoneList(cmd, targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runZoneList(cmd *cobra.Command, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "bind9.zone.list", targetName, nil)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "bind9.zone.list", r, jsonOut, printZoneList)
}

// printZoneList renders the zone list. Each row carries `{name,
// file, type}` per `_BIND9_ZONE_LIST_RESPONSE_SCHEMA`.
func printZoneList(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.zone.list — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
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
		fmt.Fprintln(w, "  (0 zones)")
		return
	}
	fmt.Fprintf(w, "%-40s %-10s %s\n", "name", "type", "file")
	for _, row := range rows {
		name := stringField(row, "name")
		zoneType := stringField(row, "type")
		file := stringField(row, "file")
		fmt.Fprintf(w, "%-40s %-10s %s\n",
			truncate(name, 40),
			truncate(zoneType, 10),
			file,
		)
	}
}

// newZoneReadCmd returns `meho bind9 zone read <zone>`. Maps to
// op_id `bind9.zone.read` with `{"zone": <name>}` params.
func newZoneReadCmd() *cobra.Command {
	var (
		targetName        string
		jsonOut           bool
		backplaneOverride string
	)
	cmd := &cobra.Command{
		Use:   "read <zone>",
		Short: "Read the records of a zone (name / ttl / class / type / rdata rows)",
		Long: "read dispatches bind9.zone.read against the connector_id=\n" +
			"\"bind9-ssh-9.x\" connector with the supplied zone name. The\n" +
			"handler resolves the zonefile path via `named-checkconf -p`\n" +
			"(no manual path-typing) and parses the file via dnspython's\n" +
			"`dns.zone.from_text`. Returns one row per rrset member —\n" +
			"an A rrset with three addresses yields three rows.\n\n" +
			"The trailing dot on <zone> is optional; the handler\n" +
			"normalises both `evba.lab` and `evba.lab.`.\n\n" +
			"Exit codes mirror meho operation call.",
		Example: "  meho bind9 zone read evba.lab --target vcf-router-bind9\n" +
			"  meho bind9 zone read evba.lab. --target vcf-router-bind9 --json",
		Args:          cobra.ExactArgs(1),
		SilenceUsage:  true,
		SilenceErrors: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			return runZoneRead(cmd, args[0], targetName, jsonOut, backplaneOverride)
		},
	}
	cmd.Flags().StringVar(&targetName, "target", "", "target slug to dispatch against")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "emit the full OperationResult envelope as JSON")
	cmd.Flags().StringVar(&backplaneOverride, "backplane", "",
		"backplane URL to query (defaults to the URL recorded by the most recent `meho login`)")
	return cmd
}

func runZoneRead(cmd *cobra.Command, zone, targetName string, jsonOut bool, backplaneOverride string) error {
	backplaneURL, err := resolveBackplane(backplaneOverride)
	if err != nil {
		return output.RenderError(cmd.ErrOrStderr(), classifyBackplaneError(err), jsonOut)
	}
	params := map[string]any{"zone": zone}
	r, err := dispatchOp(cmd.Context(), backplaneURL, "bind9.zone.read", targetName, params)
	if err != nil {
		return renderRequestError(cmd, backplaneURL, err, jsonOut)
	}
	return renderCallResult(cmd, "bind9.zone.read", r, jsonOut, printZoneRead)
}

// printZoneRead renders the zone read result. Each row carries
// `{name, ttl, class, type, rdata}` per `_BIND9_ZONE_READ_RESPONSE_SCHEMA`.
func printZoneRead(w io.Writer, r *CallResult) {
	fmt.Fprintf(w, "%s bind9.zone.read — status=%s (%.0fms)\n", ConnectorID, r.Status, r.DurationMs)
	if r.Status != "ok" {
		printErrorTrailer(w, r)
		return
	}
	body, err := decodeFlatResult(r.Result)
	if err != nil || body == nil {
		fallbackResultRender(w, r)
		return
	}
	// Surface zone + file at the head; then the rows table.
	if zone, ok := body["zone"].(string); ok && zone != "" {
		fmt.Fprintf(w, "  zone: %s\n", zone)
	}
	if file, ok := body["file"].(string); ok && file != "" {
		fmt.Fprintf(w, "  file: %s\n", file)
	}
	rowsAny, ok := body["rows"].([]any)
	if !ok {
		fallbackResultRender(w, r)
		return
	}
	if len(rowsAny) == 0 {
		fmt.Fprintln(w, "  (0 records)")
		return
	}
	fmt.Fprintf(w, "%-40s %-6s %-5s %-7s %s\n", "name", "ttl", "class", "type", "rdata")
	for _, ra := range rowsAny {
		row, ok := ra.(map[string]any)
		if !ok {
			continue
		}
		fmt.Fprintf(w, "%-40s %-6d %-5s %-7s %s\n",
			truncate(stringField(row, "name"), 40),
			intField(row, "ttl"),
			stringField(row, "class"),
			stringField(row, "type"),
			stringField(row, "rdata"),
		)
	}
}

// intField pulls an integer field from a row entry. JSON integers
// land as float64 after json.Unmarshal into map[string]any — the
// conversion is documented behaviour, not a workaround.
func intField(e map[string]any, key string) int {
	v, ok := e[key]
	if !ok {
		return 0
	}
	if f, ok := v.(float64); ok {
		return int(f)
	}
	return 0
}
